"""LLM response cache with LRU eviction and TTL support.

除了纯内存的 LLMCache 外，还提供可选的 Redis 后端（RedisCacheBackend）
以及优先 Redis、回退内存的混合缓存（HybridCache）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

# 尝试导入 redis-py，不可用时降级为纯内存缓存
try:
    import redis as _redis  # type: ignore
    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖未安装时的降级路径
    _redis = None  # type: ignore
    _REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)


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

    def __init__(self, max_size: int = 500, ttl_seconds: float = 3600, max_memory_mb: float = 100.0) -> None:
        # Enforce reasonable bounds on cache size to prevent memory issues
        self._max_size = min(max(max_size, 1), 10000)  # Clamp between 1 and 10000
        self._ttl = ttl_seconds
        self._max_memory_bytes = int(max_memory_mb * 1024 * 1024)  # Convert MB to bytes
        self._current_memory_bytes = 0
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

        # Estimate memory usage of this entry (rough approximation)
        entry_size = len(json.dumps(value).encode('utf-8'))

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
        self._store.clear()
        self._hits = 0
        self._misses = 0


class RedisCacheBackend:
    """Redis 缓存后端，封装 redis-py 操作。

    Redis 不可用（未安装依赖、连接失败或运行时异常）时优雅降级：
    所有读操作返回 None，写操作静默跳过，不影响上层调用方。
    """

    # 键名前缀，用于命名空间隔离，clear() 只清理本前缀下的键
    _KEY_PREFIX = "llmcache:"

    def __init__(self, redis_url: str, ttl_seconds: float = 3600) -> None:
        self._ttl = ttl_seconds
        self._available = False
        self._client: Optional[Any] = None
        self._hits = 0
        self._misses = 0
        if not _REDIS_AVAILABLE:
            logger.warning("RedisCacheBackend: 未安装 redis 包，后端不可用")
            return
        try:
            self._client = _redis.Redis.from_url(redis_url, decode_responses=True)
            # 通过 ping 验证连接可用
            self._client.ping()
            self._available = True
        except Exception as exc:  # 连接失败、鉴权失败等
            logger.warning("RedisCacheBackend: 无法连接 Redis (%s)，后端不可用", exc)
            self._client = None
            self._available = False

    @property
    def available(self) -> bool:
        """后端是否可用。"""
        return self._available

    def _full_key(self, key: str) -> str:
        return f"{self._KEY_PREFIX}{key}"

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """根据键读取缓存值并反序列化为 dict；不可用或未命中时返回 None。"""
        if not self._available or self._client is None:
            self._misses += 1
            return None
        try:
            raw = self._client.get(self._full_key(key))
        except Exception as exc:
            logger.warning("RedisCacheBackend.get 失败: %s", exc)
            self._misses += 1
            return None
        if raw is None:
            self._misses += 1
            return None
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("RedisCacheBackend.get 反序列化失败: %s", exc)
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: Dict[str, Any], ttl: Optional[float] = None) -> None:
        """写入缓存值并设置 TTL；不可用时静默跳过。"""
        if not self._available or self._client is None:
            return
        try:
            payload = json.dumps(value)
            ex = int(ttl if ttl is not None else self._ttl)
            if ex > 0:
                self._client.set(self._full_key(key), payload, ex=ex)
            else:
                self._client.set(self._full_key(key), payload)
        except Exception as exc:
            logger.warning("RedisCacheBackend.set 失败: %s", exc)

    def clear(self) -> None:
        """清理本前缀下的所有缓存键；不可用时静默跳过。"""
        if not self._available or self._client is None:
            self._hits = 0
            self._misses = 0
            return
        try:
            # 使用 SCAN 增量扫描，避免 KEYS 阻塞 Redis
            cursor = 0
            while True:
                cursor, keys = self._client.scan(
                    cursor=cursor, match=f"{self._KEY_PREFIX}*", count=100
                )
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except Exception as exc:
            logger.warning("RedisCacheBackend.clear 失败: %s", exc)
        self._hits = 0
        self._misses = 0

    def stats(self) -> Dict[str, Any]:
        """返回后端统计信息。"""
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "available": self._available,
        }


class HybridCache:
    """混合缓存：优先查 Redis，miss 后查内存 LLMCache；写入时同时写两层。

    构造时若提供 redis_url 且 redis 可用，则启用 Redis 层；否则降级为纯内存缓存。
    get/set/stats/clear 接口与 LLMCache 兼容，使用相同的 _make_key 逻辑。
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        max_size: int = 500,
        ttl_seconds: float = 3600,
        max_memory_mb: float = 100.0,
    ) -> None:
        self._ttl = ttl_seconds
        # 内存层始终启用，作为默认与回退
        self._memory = LLMCache(
            max_size=max_size,
            ttl_seconds=ttl_seconds,
            max_memory_mb=max_memory_mb,
        )
        self._redis: Optional[RedisCacheBackend] = None
        if redis_url:
            if not _REDIS_AVAILABLE:
                logger.warning("HybridCache: 未安装 redis 包，降级为纯内存缓存")
                return
            try:
                backend = RedisCacheBackend(redis_url=redis_url, ttl_seconds=ttl_seconds)
            except Exception as exc:
                logger.warning("HybridCache: 初始化 Redis 后端失败 (%s)，降级为纯内存缓存", exc)
                return
            if backend.available:
                self._redis = backend
                logger.info("HybridCache: 已启用 Redis 后端")
            else:
                logger.warning("HybridCache: Redis 后端不可用，降级为纯内存缓存")

    @property
    def backend(self) -> str:
        """当前生效的后端类型：启用 Redis 时为 "redis"，否则为 "memory"。"""
        return "redis" if self._redis is not None else "memory"

    @staticmethod
    def _make_key(messages, model, tools, temperature=None) -> str:
        # 复用 LLMCache 的键生成逻辑，保证两层键一致
        return LLMCache._make_key(messages, model, tools, temperature)

    def get(self, messages, model, tools=None, temperature=None) -> Optional[Dict[str, Any]]:
        key = self._make_key(messages, model, tools, temperature)
        # 优先查 Redis 层
        if self._redis is not None:
            value = self._redis.get(key)
            if value is not None:
                return value
        # Redis 未命中或不可用，回退到内存层
        return self._memory.get(messages, model, tools, temperature)

    def set(self, messages, model, tools, value: Dict[str, Any], temperature=None) -> None:
        key = self._make_key(messages, model, tools, temperature)
        # 同时写入内存层与 Redis 层
        self._memory.set(messages, model, tools, value, temperature)
        if self._redis is not None:
            self._redis.set(key, value, self._ttl)

    def stats(self) -> Dict[str, Any]:
        # 以内存层统计为基础，附加后端标识与 Redis 层统计
        result: Dict[str, Any] = dict(self._memory.stats())
        result["backend"] = self.backend
        if self._redis is not None:
            result["redis"] = self._redis.stats()
        return result

    def clear(self) -> None:
        self._memory.clear()
        if self._redis is not None:
            self._redis.clear()
