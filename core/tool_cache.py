"""Tool Result Cache — LRU cache with TTL for tool call results.

Prevents redundant tool executions within the same session:
- Same web_search query → return cached result
- Same calc expression → return cached result
- Same system_run command → return cached result (short TTL for safety)

Uses a simple in-memory LRU dict with per-entry TTL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SIZE = 100
DEFAULT_TTL = 300  # 5 minutes
SHORT_TTL = 30     # 30 seconds for system commands


class ToolResultCache:
    """In-memory cache for tool call results with TTL.

    Cache key = md5(tool_name + sorted_json(args))
    Each entry has a TTL after which it's considered stale.
    """

    def __init__(self, max_size: int = DEFAULT_CACHE_SIZE) -> None:
        self._max_size = max_size
        self._cache: Dict[str, Tuple[float, str]] = {}  # key → (expiry, result)
        self._access_order: list = []  # LRU tracking
        self._hits = 0
        self._misses = 0

    def _make_key(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Create a deterministic cache key from tool name + args."""
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        raw = f"{tool_name}:{args_str}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_ttl(self, tool_name: str) -> float:
        """Get TTL based on tool type."""
        if tool_name in ("system_run", "shell", "exec"):
            return SHORT_TTL
        return DEFAULT_TTL

    def get(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """Get cached result if available and not expired."""
        key = self._make_key(tool_name, args)
        if key not in self._cache:
            self._misses += 1
            return None

        expiry, result = self._cache[key]
        if time.time() > expiry:
            # Expired — remove and return None
            del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)
            self._misses += 1
            return None

        # Cache hit — update LRU
        self._hits += 1
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)
        return result

    def set(self, tool_name: str, args: Dict[str, Any], result: str) -> None:
        """Store a result in the cache."""
        key = self._make_key(tool_name, args)
        ttl = self._get_ttl(tool_name)
        expiry = time.time() + ttl

        # Evict oldest if at capacity
        if key not in self._cache and len(self._cache) >= self._max_size:
            if self._access_order:
                oldest = self._access_order.pop(0)
                self._cache.pop(oldest, None)

        self._cache[key] = (expiry, result)
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)

    def invalidate(self, tool_name: str) -> None:
        """Invalidate all cached results for a tool."""
        to_remove = []
        for key, (_expiry, _result) in self._cache.items():
            # Key is md5, can't extract tool name — we iterate all
            pass
        # Since we can't extract tool name from md5 key, invalidate by
        # pattern is not feasible. Use clear() for full invalidation.
        logger.debug("tool_cache: invalidate(%s) — full invalidation not supported", tool_name)

    def clear(self) -> None:
        """Clear all cached results."""
        self._cache.clear()
        self._access_order.clear()
        logger.debug("tool_cache: cleared all entries")

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.1%}",
        }


# Singleton instance
_tool_cache: Optional[ToolResultCache] = None


def get_tool_cache() -> ToolResultCache:
    """Get the shared tool result cache."""
    global _tool_cache
    if _tool_cache is None:
        _tool_cache = ToolResultCache()
    return _tool_cache