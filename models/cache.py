"""LLM response cache with LRU eviction and TTL support."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Dict, Optional


class _CacheEntry:
    """Simple LRU cache entry with TTL support."""

    def __init__(self, value: Dict[str, Any], ttl: float = 3600) -> None:
        self.value = value
        self.created_at = time.time()
        self.ttl = ttl
        self.hits = 0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl


class LLMCache:
    """LRU cache with TTL for LLM responses."""

    def __init__(self, max_size: int = 500, ttl_seconds: float = 3600) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _make_key(messages, model, tools, temperature=None) -> str:
        payload = json.dumps({
            "messages": messages,
            "model": model,
            "tools": tools,
            "temperature": temperature,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def get(self, messages, model, tools=None, temperature=None) -> Optional[Dict[str, Any]]:
        key = self._make_key(messages, model, tools, temperature)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if entry.is_expired():
            del self._store[key]
            self._misses += 1
            return None
        self._hits += 1
        entry.hits += 1
        # Move to end (most recently used)
        self._store.move_to_end(key)
        return entry.value

    def set(self, messages, model, tools, value: Dict[str, Any], temperature=None) -> None:
        key = self._make_key(messages, model, tools, temperature)
        entry = _CacheEntry(value, self._ttl)
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = entry
        if len(self._store) > self._max_size:
            # Evict oldest
            self._store.popitem(last=False)

    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "size": len(self._store),
            "max_size": self._max_size,
        }

    def clear(self) -> None:
        self._store.clear()
        self._hits = 0
        self._misses = 0