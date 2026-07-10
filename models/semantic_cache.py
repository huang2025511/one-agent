"""Semantic Cache — embedding-based similarity cache for LLM responses.

Enhances the exact-match LLMCache with semantic similarity lookup:
- Exact match first (fast, zero cost)
- If no exact match, try semantic similarity
- Similarity above threshold → return cached result
- Below threshold → forward to LLM and cache result

Uses the same embedding model as memory search for consistency.
Gracefully degrades to exact-match-only if sentence-transformers is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_SEMANTIC_THRESHOLD = 0.92  # Cosine similarity threshold for cache hit
DEFAULT_MAX_SEMANTIC_CANDIDATES = 20  # Max candidates to check for semantic match


class SemanticCacheEntry:
    """Cache entry with embedding vector for semantic lookup."""

    def __init__(
        self,
        value: Dict[str, Any],
        prompt_text: str,
        embedding: Optional[List[float]] = None,
        ttl: float = 3600,
    ) -> None:
        self.value = value
        self.prompt_text = prompt_text
        self.embedding = embedding
        self.created_at = time.time()
        self.ttl = ttl
        self.hits = 0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl


class SemanticCache:
    """Hybrid LLM cache: exact match + semantic similarity.

    Two-tier lookup:
    1. Exact hash match (instant, 100% accuracy)
    2. Semantic similarity search (slower, configurable threshold)

    Semantic search uses cosine similarity over prompt embeddings.
    Falls back to exact-match-only if embeddings are unavailable.
    """

    def __init__(
        self,
        max_size: int = 500,
        ttl_seconds: float = 3600,
        semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
        embedding_provider: Optional[Any] = None,
    ) -> None:
        self._max_size = min(max(max_size, 1), 10000)
        self._ttl = ttl_seconds
        self._semantic_threshold = semantic_threshold
        self._embedding_provider = embedding_provider

        # Exact match store (hash → entry)
        self._exact_store: OrderedDict[str, SemanticCacheEntry] = OrderedDict()

        # Semantic index: list of entries for similarity search
        # Kept in sync with exact_store
        self._semantic_index: List[SemanticCacheEntry] = []

        self._hits_exact = 0
        self._hits_semantic = 0
        self._misses = 0
        self._lock = threading.RLock()

        # Lazy embedding model reference
        self._embedding_model = None
        self._embedding_available: Optional[bool] = None  # None = not checked yet

    # ========================================================= Embedding helpers

    def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Get embedding for text, trying multiple sources."""
        # Try external provider first
        if self._embedding_provider is not None:
            try:
                if hasattr(self._embedding_provider, "embed"):
                    return self._embedding_provider.embed(text)
            except Exception as exc:
                logger.debug("ignored non-critical error: %s", exc)

        # Try sentence-transformers directly (lazy import)
        if self._embedding_available is None:
            try:
                from sentence_transformers import SentenceTransformer

                self._embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
                self._embedding_available = True
                logger.debug("Semantic cache: loaded embedding model")
            except ImportError:
                self._embedding_available = False
                logger.debug("Semantic cache: sentence-transformers not available, exact match only")
            except Exception as exc:
                self._embedding_available = False
                logger.debug("Semantic cache: embedding model unavailable: %s", exc)

        if not self._embedding_available or self._embedding_model is None:
            return None

        try:
            embedding = self._embedding_model.encode(text, convert_to_numpy=True)
            return embedding.tolist()
        except Exception as exc:
            logger.debug("Semantic cache embedding failed: %s", exc)
            return None

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ========================================================= Key extraction

    @staticmethod
    def _extract_user_prompt(messages: List[Dict[str, Any]]) -> str:
        """Extract the last user message as the semantic lookup key.

        We use the last user message rather than the full conversation because:
        1. Full conversation hashing is already handled by exact match
        2. Semantic similarity on the user question is what matters for reuse
        3. Keeps embeddings smaller and faster
        """
        if not messages:
            return ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content.strip()
                # Handle list-type content (multimodal)
                if isinstance(content, list):
                    text_parts = [
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ]
                    return " ".join(text_parts).strip()
        return ""

    @staticmethod
    def _make_exact_key(messages, model, tools, temperature=None) -> str:
        """Make exact-match hash key (same as LLMCache)."""
        payload = json.dumps({
            "messages": messages,
            "model": model,
            "tools": tools,
            "temperature": temperature,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    # ========================================================= Public API

    def get(
        self,
        messages,
        model: str,
        tools=None,
        temperature: Optional[float] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """Look up a cached response.

        Returns (result, hit_type) where hit_type is:
        - "exact": exact hash match
        - "semantic": semantic similarity match
        - "miss": not found
        """
        exact_key = self._make_exact_key(messages, model, tools, temperature)

        with self._lock:
            # Tier 1: exact match
            entry = self._exact_store.get(exact_key)
            if entry is not None and not entry.is_expired():
                self._hits_exact += 1
                entry.hits += 1
                self._exact_store.move_to_end(exact_key)
                return entry.value, "exact"

            if entry is not None and entry.is_expired():
                del self._exact_store[exact_key]
                # Also remove from semantic index (will be rebuilt lazily if needed)
                self._semantic_index = [e for e in self._semantic_index if e is not entry]

            # Tier 2: semantic match (only if we have a user prompt to embed)
            prompt_text = self._extract_user_prompt(messages)
            if not prompt_text or len(prompt_text) < 10:
                # Too short for meaningful semantic match
                self._misses += 1
                return None, "miss"

            # Only do semantic search if embeddings are available (or not yet checked)
            if self._embedding_available is False and self._embedding_provider is None:
                self._misses += 1
                return None, "miss"

            if not self._semantic_index:
                self._misses += 1
                return None, "miss"

            embedding = self._get_embedding(prompt_text)
            if embedding is None:
                self._misses += 1
                return None, "miss"

            # Find best semantic match
            best_score = 0.0
            best_entry = None
            candidates = self._semantic_index[-DEFAULT_MAX_SEMANTIC_CANDIDATES:]

            for candidate in candidates:
                if candidate.is_expired():
                    continue
                if candidate.embedding is None:
                    continue
                score = self._cosine_similarity(embedding, candidate.embedding)
                if score > best_score:
                    best_score = score
                    best_entry = candidate

            if best_entry is not None and best_score >= self._semantic_threshold:
                self._hits_semantic += 1
                best_entry.hits += 1
                logger.debug(
                    "Semantic cache hit (score=%.3f): %s → %s",
                    best_score,
                    prompt_text[:50],
                    best_entry.prompt_text[:50],
                )
                return best_entry.value, "semantic"

            self._misses += 1
            return None, "miss"

    def set(
        self,
        messages,
        model: str,
        tools,
        value: Dict[str, Any],
        temperature: Optional[float] = None,
    ) -> None:
        """Store a response in the cache."""
        exact_key = self._make_exact_key(messages, model, tools, temperature)
        prompt_text = self._extract_user_prompt(messages)

        # Try to get embedding (best-effort, may be None)
        embedding = None
        if prompt_text and len(prompt_text) >= 10:
            embedding = self._get_embedding(prompt_text)

        entry = SemanticCacheEntry(value, prompt_text, embedding, self._ttl)

        with self._lock:
            # Remove old entry if exists
            if exact_key in self._exact_store:
                old_entry = self._exact_store[exact_key]
                self._semantic_index = [e for e in self._semantic_index if e is not old_entry]
                del self._exact_store[exact_key]

            # Evict if needed
            while len(self._exact_store) >= self._max_size:
                evicted_key, evicted_entry = self._exact_store.popitem(last=False)
                self._semantic_index = [e for e in self._semantic_index if e is not evicted_entry]

            # Add new entry
            self._exact_store[exact_key] = entry
            if embedding is not None:
                self._semantic_index.append(entry)

            # Keep semantic index size bounded
            if len(self._semantic_index) > self._max_size:
                # Remove oldest entries (those at the front)
                overflow = len(self._semantic_index) - self._max_size
                self._semantic_index = self._semantic_index[overflow:]

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = self._hits_exact + self._hits_semantic + self._misses
            return {
                "exact_hits": self._hits_exact,
                "semantic_hits": self._hits_semantic,
                "total_hits": self._hits_exact + self._hits_semantic,
                "misses": self._misses,
                "hit_rate": round((self._hits_exact + self._hits_semantic) / total, 3) if total > 0 else 0.0,
                "semantic_hit_rate": round(self._hits_semantic / total, 3) if total > 0 else 0.0,
                "size": len(self._exact_store),
                "semantic_index_size": len(self._semantic_index),
                "max_size": self._max_size,
                "semantic_threshold": self._semantic_threshold,
                "embedding_available": self._embedding_available if self._embedding_available is not None else False,
            }

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._exact_store.clear()
            self._semantic_index.clear()
            self._hits_exact = 0
            self._hits_semantic = 0
            self._misses = 0