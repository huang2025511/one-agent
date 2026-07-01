"""Embedding-based semantic search for memory.

Provides vector embeddings for semantic similarity search, complementing
the existing FTS5 keyword search. Uses a lightweight local embedding model
to avoid external API dependencies.

Architecture:
- Embedding model: sentence-transformers/all-MiniLM-L6-v2 (384 dims)
- Vector storage: SQLite with cosine similarity
- Hybrid search: Combine FTS5 keyword + embedding semantic scores
"""

from __future__ import annotations

import logging
import struct
import time
from typing import List, Optional, Tuple

from .base_store import BaseSQLiteStore

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

logger = logging.getLogger(__name__)

# Embedding model configuration
EMBEDDING_DIM = 384
MODEL_NAME = "all-MiniLM-L6-v2"
MODEL_LOAD_TIMEOUT = 30.0


def _dot_product(a: List[float], b: List[float]) -> float:
    """Calculate dot product of two vectors."""
    return sum(x * y for x, y in zip(a, b, strict=False))


def _norm(v: List[float]) -> float:
    """Calculate L2 norm of a vector."""
    return sum(x * x for x in v) ** 0.5


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    norm_a = _norm(a)
    norm_b = _norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return _dot_product(a, b) / (norm_a * norm_b)


def _vector_to_blob(vector: List[float]) -> bytes:
    """Convert list of floats to binary blob."""
    return struct.pack(f'{len(vector)}f', *vector)


def _blob_to_vector(blob: bytes) -> List[float]:
    """Convert binary blob to list of floats."""
    count = len(blob) // 4
    return list(struct.unpack(f'{count}f', blob))


class EmbeddingStore(BaseSQLiteStore):
    """Vector store for semantic memory search."""

    def __init__(
        self, db_path: str = "data/memory/embeddings.db",
    ):
        """Initialize embedding store.

        Args:
            db_path: Path to SQLite database for vector storage
        """
        self._model = None
        self._model_loaded = False
        self._model_not_installed = False
        self._model_load_attempts = 0
        # In-memory vector cache as parallel arrays for fast cosine search.
        # _cache_ids:    [memory_id, ...]
        # _cache_vecs:   [vector_list, ...]
        # _cache_norms:  [precomputed_norm, ...]  — avoids recomputing
        #                L2 norm on every search (was the hot-path bottleneck).
        # All three are invalidated together on store()/delete().
        self._cache_ids: List[str] = []
        self._cache_vecs: List[List[float]] = []
        self._cache_norms: List[float] = []
        self._cache_loaded: bool = False
        # Legacy alias kept for backward-compat with code that checks _vector_cache
        self._vector_cache: Optional[List[Tuple[str, List[float]]]] = None
        super().__init__(db_path)
        # Model loading is deferred to first embed() call — loading
        # sentence-transformers at construction time added seconds to
        # startup even when memory/embedding search was never used.

    def _init_db(self):
        """Initialize SQLite database with vector storage schema."""
        # Create tables
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL UNIQUE,
                vector BLOB NOT NULL,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_id ON embeddings(memory_id);
        """)
        self._conn.commit()
        logger.debug("Embedding store initialized at %s", self.db_path)

    def _load_model(self):
        """Load the embedding model lazily with timeout."""
        try:
            import concurrent.futures

            from sentence_transformers import SentenceTransformer

            # Load model with timeout to prevent blocking
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(SentenceTransformer, MODEL_NAME)
                try:
                    self._model = future.result(timeout=MODEL_LOAD_TIMEOUT)
                    logger.debug("Loaded embedding model: %s", MODEL_NAME)
                except concurrent.futures.TimeoutError:
                    logger.warning(
                        "Embedding model loading timed out after %.0fs. "
                        "Semantic search will be disabled.",
                        MODEL_LOAD_TIMEOUT
                    )
                    self._model = None
        except ImportError:
            logger.warning(
                "sentence-transformers not installed. "
                "Run: pip install sentence-transformers"
            )
            self._model = None
            self._model_not_installed = True
        except Exception as e:
            logger.warning("Failed to load embedding model: %s", e)
            self._model = None

    def _try_load_model(self) -> None:
        """Attempt to load the embedding model with bounded retries.

        Distinguishes "not installed" (permanently unavailable, no retry)
        from transient failures (retry up to 3 times before giving up).
        ``_model_loaded`` is only set True once the model is available or
        after retries are exhausted, so a transient failure on the first
        call does not permanently disable semantic search.
        """
        if self._model_not_installed:
            self._model_loaded = True
            return
        self._model_load_attempts += 1
        self._load_model()
        if self._model is not None:
            self._model_loaded = True
            return
        if self._model_not_installed:
            self._model_loaded = True
            return
        if self._model_load_attempts >= 3:
            self._model_loaded = True
            logger.warning(
                "Embedding model failed to load after %d attempts; "
                "disabling semantic search for this session.",
                self._model_load_attempts,
            )

    def embed(self, text: str) -> Optional[List[float]]:
        """Generate embedding vector for text.

        Args:
            text: Input text to embed

        Returns:
            list of floats (EMBEDDING_DIM,) or None if model unavailable
        """
        if not text:
            raise ValueError("text cannot be empty")
        if not isinstance(text, str):
            raise ValueError("text must be a string")

        # Lazy-load the model on first use instead of in __init__.
        # _model_loaded is only set True on success or after we give up
        # retrying, so a transient load failure is retried on later calls.
        if not self._model_loaded:
            self._try_load_model()
        if self._model is None:
            return None
        try:
            embedding = self._model.encode(text, convert_to_numpy=True)
            return embedding.tolist()
        except Exception as e:
            logger.warning("Embedding failed: %s", e)
            return None

    def store(self, memory_id: str, vector: List[float]):
        """Store embedding vector for a memory item.

        Args:
            memory_id: Unique identifier for the memory
            vector: Embedding vector (EMBEDDING_DIM,)
        """
        if not memory_id:
            raise ValueError("memory_id cannot be empty")
        if not isinstance(memory_id, str):
            raise ValueError("memory_id must be a string")
        if vector is None:
            raise ValueError("vector cannot be None")
        if not isinstance(vector, list):
            raise ValueError("vector must be a list")
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(f"vector must have {EMBEDDING_DIM} dimensions")

        with self._write_lock:
            try:
                vector_blob = _vector_to_blob(vector)
                self._conn.execute(
                    """INSERT OR REPLACE INTO embeddings (memory_id, vector, created_at)
                       VALUES (?, ?, ?)""",
                    (memory_id, vector_blob, time.time()),
                )
                self._conn.commit()
                # Incrementally update the cache instead of invalidating it
                # entirely. Lists are rebuilt as NEW objects (never mutated
                # in place) so a concurrent search() that already snapshotted
                # the old references keeps a consistent view. INSERT OR
                # REPLACE may update an existing memory_id, so handle both
                # the append and the in-cache update cases.
                if self._cache_loaded:
                    cached_vec = list(vector)
                    cached_norm = _norm(cached_vec)
                    try:
                        idx = self._cache_ids.index(memory_id)
                    except ValueError:
                        idx = -1
                    if idx >= 0:
                        self._cache_ids = [
                            *self._cache_ids[:idx], memory_id,
                            *self._cache_ids[idx + 1:],
                        ]
                        self._cache_vecs = [
                            *self._cache_vecs[:idx], cached_vec,
                            *self._cache_vecs[idx + 1:],
                        ]
                        self._cache_norms = [
                            *self._cache_norms[:idx], cached_norm,
                            *self._cache_norms[idx + 1:],
                        ]
                    else:
                        self._cache_ids = [*self._cache_ids, memory_id]
                        self._cache_vecs = [*self._cache_vecs, cached_vec]
                        self._cache_norms = [*self._cache_norms, cached_norm]
                    if self._vector_cache is not None:
                        self._vector_cache = [
                            (mid, cached_vec) if mid == memory_id else (mid, vec)
                            for mid, vec in self._vector_cache
                        ] if idx >= 0 else [
                            *self._vector_cache, (memory_id, cached_vec)
                        ]
            except Exception as e:
                logger.warning("Failed to store embedding for %s: %s", memory_id, e)

    def search(self, query_vector: List[float], top_k: int = 10) -> List[Tuple[str, float]]:
        """Search for similar memories using cosine similarity.

        Uses an in-memory cache of all vectors with precomputed L2 norms
        to avoid repeated SQLite reads and redundant norm calculations.
        The cache is invalidated on store()/delete().

        Args:
            query_vector: Query embedding vector
            top_k: Number of results to return

        Returns:
            List of (memory_id, similarity_score) tuples, sorted by score desc
        """
        if query_vector is None or not isinstance(query_vector, list):
            return []
        if top_k <= 0:
            return []

        try:
            # Snapshot the cache references under the write lock so a
            # concurrent store()/delete() cannot swap the underlying lists
            # between our reads (which would otherwise cause an IndexError
            # when vecs[i] is accessed after the lists were cleared). After
            # the snapshot we compute without holding the lock; store/delete
            # never mutate the old lists in place, only replace references.
            with self._write_lock:
                if not self._cache_loaded:
                    self._load_vector_cache()
                ids = self._cache_ids
                vecs = self._cache_vecs
                norms = self._cache_norms

            if not ids:
                return []

            # Precompute query norm once (was recomputed per-vector before).
            query_norm = _norm(query_vector)
            if query_norm == 0:
                return []

            results: List[Tuple[str, float]] = []
            n = len(ids)
            # Cosine = dot / (norm_a * norm_b). Norms are precomputed,
            # so the inner loop only does the dot product.
            if _HAS_NUMPY and n > 0:
                matrix = np.asarray(vecs, dtype=np.float64)        # (N, D)
                q = np.asarray(query_vector, dtype=np.float64)      # (D,)
                dots = matrix.dot(q)                                # (N,)
                norms_arr = np.asarray(norms, dtype=np.float64)    # (N,)
                denom = query_norm * norms_arr
                # Avoid divide-by-zero; entries with zero norm yield sim 0.
                sims = np.where(denom > 0, dots / denom, 0.0)
                for i in range(n):
                    results.append((ids[i], float(sims[i])))
            else:
                for i in range(n):
                    stored_norm = norms[i]
                    if stored_norm == 0:
                        continue
                    sim = _dot_product(query_vector, vecs[i]) / (query_norm * stored_norm)
                    results.append((ids[i], sim))

            results.sort(key=lambda x: x[1], reverse=True)
            return results[:top_k]

        except Exception as e:
            logger.warning("Embedding search failed: %s", e)
            return []

    def _load_vector_cache(self) -> None:
        """Load all vectors from SQLite into the parallel-array cache.

        Called lazily on first ``search()``. Precomputes each vector's L2
        norm so subsequent searches only need the dot product.
        """
        cursor = self._conn.execute("SELECT memory_id, vector FROM embeddings")
        ids: List[str] = []
        vecs: List[List[float]] = []
        norms: List[float] = []
        for row in cursor:
            memory_id = row["memory_id"]
            stored_vector = _blob_to_vector(row["vector"])
            if stored_vector:
                ids.append(memory_id)
                vecs.append(stored_vector)
                norms.append(_norm(stored_vector))
        self._cache_ids = ids
        self._cache_vecs = vecs
        self._cache_norms = norms
        self._cache_loaded = True
        # Maintain legacy alias for any code that inspects _vector_cache
        self._vector_cache = list(zip(ids, vecs)) if ids else None

    def delete(self, memory_id: str):
        """Delete embedding for a memory item.

        Args:
            memory_id: Unique identifier for the memory
        """
        if not memory_id:
            raise ValueError("memory_id cannot be empty")
        if not isinstance(memory_id, str):
            raise ValueError("memory_id must be a string")

        with self._write_lock:
            try:
                self._conn.execute(
                    "DELETE FROM embeddings WHERE memory_id = ?",
                    (memory_id,),
                )
                self._conn.commit()
                # Remove only the affected entry from the cache instead of
                # invalidating the whole cache (avoids a full reload + norm
                # recomputation on the next search). Lists are rebuilt as
                # new objects so a concurrent search() keeps a consistent
                # view of the old snapshot.
                if self._cache_loaded:
                    try:
                        idx = self._cache_ids.index(memory_id)
                    except ValueError:
                        idx = -1
                    if idx >= 0:
                        self._cache_ids = [
                            *self._cache_ids[:idx], *self._cache_ids[idx + 1:],
                        ]
                        self._cache_vecs = [
                            *self._cache_vecs[:idx], *self._cache_vecs[idx + 1:],
                        ]
                        self._cache_norms = [
                            *self._cache_norms[:idx], *self._cache_norms[idx + 1:],
                        ]
                        if self._vector_cache is not None:
                            self._vector_cache = [
                                (mid, vec)
                                for mid, vec in self._vector_cache
                                if mid != memory_id
                            ]
            except Exception as e:
                logger.warning("Failed to delete embedding for %s: %s", memory_id, e)

    def count(self) -> int:
        """Return total number of stored embeddings."""
        try:
            cursor = self._conn.execute("SELECT COUNT(*) FROM embeddings")
            return cursor.fetchone()[0]
        except Exception as e:
            logger.warning("Failed to count embeddings: %s", e)
            return 0
