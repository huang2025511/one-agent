"""Tool Result Cache — LRU cache with TTL for tool call results.

Prevents redundant tool executions within the same session:
- Same web_search query → return cached result
- Same calc expression → return cached result
- Same system_run command → return cached result (short TTL for safety)

Uses OrderedDict for O(1) LRU operations (move_to_end / popitem).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SIZE = 100
DEFAULT_TTL = 300  # 5 minutes
SHORT_TTL = 30     # 30 seconds for system commands


class ToolResultCache:
    """In-memory cache for tool call results with TTL.

    Cache key = "tool_name:md5(args)" — 保留 tool_name 前缀以支持按工具失效。
    使用 OrderedDict 实现 O(1) LRU：
      - get hit → move_to_end(key)
      - set 满 → popitem(last=False) 淘汰最久未用
    """

    def __init__(self, max_size: int = DEFAULT_CACHE_SIZE) -> None:
        self._max_size = max_size
        # key → (expiry, result); OrderedDict 维护 LRU 顺序（末尾=最近使用）
        self._cache: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
        self._hits = 0
        self._misses = 0

    def _make_key(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Create a deterministic cache key from tool name + args.

        保留 tool_name 前缀, 让 invalidate() 能按工具批量删除。
        """
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        args_hash = hashlib.md5(args_str.encode()).hexdigest()
        return f"{tool_name}:{args_hash}"

    def _get_ttl(self, tool_name: str) -> float:
        """Get TTL based on tool type."""
        if tool_name in ("system_run", "shell", "exec"):
            return SHORT_TTL
        return DEFAULT_TTL

    def get(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """Get cached result if available and not expired. O(1)."""
        key = self._make_key(tool_name, args)
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None

        expiry, result = entry
        if time.time() > expiry:
            # 过期 — 删除并返回 None
            del self._cache[key]
            self._misses += 1
            return None

        # 命中 — 移到末尾（最近使用）, O(1)
        self._hits += 1
        self._cache.move_to_end(key)
        return result

    def set(self, tool_name: str, args: Dict[str, Any], result: str) -> None:
        """Store a result in the cache. O(1)."""
        key = self._make_key(tool_name, args)
        ttl = self._get_ttl(tool_name)
        expiry = time.time() + ttl

        # 容量满且是新 key → 淘汰最久未用 (头部), O(1)
        if key not in self._cache and len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)

        self._cache[key] = (expiry, result)
        # 移到末尾（最近使用）, O(1)
        self._cache.move_to_end(key)

    def invalidate(self, tool_name: str) -> int:
        """Invalidate all cached results for a tool.

        修复：之前 key 是纯 md5, 无法反查工具名, invalidate 沦为空操作。
        现在 key 格式为 "tool_name:hash", 可按前缀批量删除。
        返回删除的条目数。
        """
        prefix = f"{tool_name}:"
        removed = 0
        # OrderedDict 不支持迭代中删除, 先收集 key
        keys_to_remove = [k for k in self._cache.keys() if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._cache[k]
            removed += 1
        if removed:
            logger.debug("tool_cache: invalidated %d entries for %s", removed, tool_name)
        return removed

    def clear(self) -> None:
        """Clear all cached results."""
        self._cache.clear()
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