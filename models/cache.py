"""LLM response cache with LRU eviction and TTL support."""

from __future__ import annotations

import hashlib
import json
import threading
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
    """LRU cache with TTL for LLM responses.

    Thread-safe: all read/write operations are protected by a lock, since
    the cache may be accessed concurrently when LLM providers run in a
    thread pool (e.g. via asyncio.to_thread).
    """

    def __init__(self, max_size: int = 500, ttl_seconds: float = 3600, max_memory_mb: float = 100.0) -> None:
        # Enforce reasonable bounds on cache size to prevent memory issues
        self._max_size = min(max(max_size, 1), 10000)  # Clamp between 1 and 10000
        self._ttl = ttl_seconds
        self._max_memory_bytes = int(max_memory_mb * 1024 * 1024)  # Convert MB to bytes
        self._current_memory_bytes = 0
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    @staticmethod
    def _make_key(messages, model, tools, temperature=None) -> str:
        # 规范化 tools：None 和 [] 都视为空列表，避免 get(None) 和
        # set([]) 产生不同 cache key 导致 cache 永远 miss。
        # 这是修复 5 的根本修复——chat_completion 传 tools or [] 只是
        # 上层兜底，_make_key 本身规范化才能彻底保证一致性。
        normalized_tools = tools or []
        payload = json.dumps({
            "messages": messages,
            "model": model,
            "tools": normalized_tools,
            "temperature": temperature,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def get(self, messages, model, tools=None, temperature=None) -> Optional[Dict[str, Any]]:
        key = self._make_key(messages, model, tools, temperature)
        with self._lock:
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

        # Estimate memory usage of this entry (rough approximation)
        # Serialize once and reuse to avoid repeated json.dumps calls.
        serialized = json.dumps(value).encode('utf-8')
        entry_size = len(serialized)

        with self._lock:
            if key in self._store:
                # Update existing entry
                old_entry = self._store[key]
                old_size = len(json.dumps(old_entry.value).encode('utf-8'))
                self._current_memory_bytes -= old_size
                self._store.move_to_end(key)
            else:
                # New entry - check if we need to evict
                while self._store and (len(self._store) >= self._max_size or
                                       self._current_memory_bytes + entry_size > self._max_memory_bytes):
                    # Evict oldest entry
                    evicted_key, evicted_entry = self._store.popitem(last=False)
                    evicted_size = len(json.dumps(evicted_entry.value).encode('utf-8'))
                    self._current_memory_bytes -= evicted_size

            self._store[key] = entry
            self._current_memory_bytes += entry_size

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
                "size": len(self._store),
                "max_size": self._max_size,
                "memory_used_mb": round(self._current_memory_bytes / (1024 * 1024), 2),
                "max_memory_mb": round(self._max_memory_bytes / (1024 * 1024), 2),
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._current_memory_bytes = 0
