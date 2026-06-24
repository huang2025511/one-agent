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
        # In-memory vector cache: {(memory_id, vector_list)}.
        # Avoids reloading all vectors from SQLite on every search.
        # Invalidated on store()/delete().
        self._vector_cache: Optional[List[Tuple[str, List[float]]]] = None
        super().__init__(db_path)
        self._load_model()

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
        except Exception as e:
            logger.warning("Failed to load embedding model: %s", e)
            self._model = None

    def embed(self, text: str) -> Optional[List[float]]:
        """Generate embedding vector for text.

        Args:
            text: Input text to embed

        Returns:
            list of floats (EMBEDDING_DIM,) or None if model unavailable
        """
        assert text, "text cannot be empty"
        assert isinstance(text, str), "text must be a string"

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
        assert memory_id, "memory_id cannot be empty"
        assert isinstance(memory_id, str), "memory_id must be a string"
        assert vector is not None, "vector cannot be None"
        assert isinstance(vector, list), "vector must be a list"
        assert len(vector) == EMBEDDING_DIM, f"vector must have {EMBEDDING_DIM} dimensions"

        with self._write_lock:
            try:
                vector_blob = _vector_to_blob(vector)
                self._conn.execute(
                    """INSERT OR REPLACE INTO embeddings (memory_id, vector, created_at)
                       VALUES (?, ?, ?)""",
                    (memory_id, vector_blob, time.time()),
                )
                self._conn.commit()
                # Invalidate cache so next search reloads
                self._vector_cache = None
            except Exception as e:
                logger.warning("Failed to store embedding for %s: %s", memory_id, e)

    def search(self, query_vector: List[float], top_k: int = 10) -> List[Tuple[str, float]]:
        """Search for similar memories using cosine similarity.

        Uses an in-memory cache of all vectors to avoid repeated SQLite
        reads. The cache is invalidated on store()/delete().

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
            # Load vectors into cache if not yet loaded.
            # Capture the cache reference into a local variable so that a
            # concurrent store()/delete() setting self._vector_cache = None
            # cannot cause TypeError during iteration below (TOCTOU fix).
            cache = self._vector_cache
            if cache is None:
                cursor = self._conn.execute(
                    "SELECT memory_id, vector FROM embeddings"
                )
                cache = []
                for row in cursor:
                    memory_id = row["memory_id"]
                    stored_vector = _blob_to_vector(row["vector"])
                    if stored_vector:
                        cache.append((memory_id, stored_vector))
                self._vector_cache = cache

            # Compute cosine similarity against cached vectors.
            # Iterate over the local `cache` reference, NOT self._vector_cache,
            # so we are immune to concurrent invalidation.
            results = []
            for memory_id, stored_vector in cache:
                similarity = _cosine_similarity(query_vector, stored_vector)
                results.append((memory_id, similarity))

            # Sort by similarity descending
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:top_k]

        except Exception as e:
            logger.warning("Embedding search failed: %s", e)
            return []

    def delete(self, memory_id: str):
        """Delete embedding for a memory item.

        Args:
            memory_id: Unique identifier for the memory
        """
        assert memory_id, "memory_id cannot be empty"
        assert isinstance(memory_id, str), "memory_id must be a string"

        with self._write_lock:
            try:
                self._conn.execute(
                    "DELETE FROM embeddings WHERE memory_id = ?",
                    (memory_id,),
                )
                self._conn.commit()
                # Invalidate cache so next search reloads
                self._vector_cache = None
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
