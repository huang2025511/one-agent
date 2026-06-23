"""性能优化模块 — 提供缓存优化、资源池管理和异步优化功能。

提供：
  - 多级缓存系统（内存/磁盘/Redis）
  - 连接池管理
  - 异步任务调度优化
  - 性能监控和分析
  - 自动资源清理
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Dict, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class CacheStats:
    """缓存统计信息。"""
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    size: int = 0
    max_size: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class MultiLevelCache:
    """多级缓存系统 — 支持内存、磁盘和Redis缓存。"""

    def __init__(self, config: Dict[str, Any] = None):
        config = config or {}
        self._memory_cache: Dict[str, Any] = {}
        self._memory_ttl: Dict[str, float] = {}
        self._memory_max_size = config.get("memory_max_size", 1000)
        self._memory_ttl = config.get("memory_ttl", 3600)  # 1小时
        
        self._disk_enabled = config.get("disk_enabled", False)
        self._disk_path = config.get("disk_path", "data/cache")
        self._disk_max_size = config.get("disk_max_size", 100 * 1024 * 1024)  # 100MB
        
        self._redis_enabled = config.get("redis_enabled", False)
        self._redis_host = config.get("redis_host", "localhost")
        self._redis_port = config.get("redis_port", 6379)
        self._redis_client = None
        
        self._stats = CacheStats(max_size=self._memory_max_size)
        
        if self._disk_enabled:
            os.makedirs(self._disk_path, exist_ok=True)
        
        if self._redis_enabled:
            self._init_redis()

    def _init_redis(self):
        """初始化Redis客户端。"""
        try:
            import redis
            self._redis_client = redis.Redis(
                host=self._redis_host,
                port=self._redis_port,
                decode_responses=True
            )
            self._redis_client.ping()
            logger.info("Redis cache connected")
        except Exception as exc:
            logger.warning("Failed to connect to Redis: %s", exc)
            self._redis_enabled = False

    def _compute_key(self, key: str) -> str:
        """计算缓存键的哈希值。"""
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _memory_cleanup(self):
        """清理过期和多余的缓存。"""
        now = time.time()
        # 清理过期缓存
        expired = [k for k, ttl in self._memory_ttl.items() if now > ttl]
        for k in expired:
            del self._memory_cache[k]
            del self._memory_ttl[k]
            self._stats.evictions += 1
        
        # 清理超出限制的缓存（LRU策略）
        if len(self._memory_cache) > self._memory_max_size:
            sorted_keys = sorted(self._memory_ttl.keys(), key=lambda k: self._memory_ttl[k])
            to_remove = len(self._memory_cache) - self._memory_max_size
            for k in sorted_keys[:to_remove]:
                del self._memory_cache[k]
                del self._memory_ttl[k]
                self._stats.evictions += 1
        
        self._stats.size = len(self._memory_cache)

    def get(self, key: str) -> Optional[Any]:
        """从缓存获取值。"""
        cache_key = self._compute_key(key)
        now = time.time()
        
        # 先查内存缓存
        if cache_key in self._memory_cache:
            if now < self._memory_ttl.get(cache_key, 0):
                self._stats.hits += 1
                return self._memory_cache[cache_key]
            else:
                # 过期了，删除
                del self._memory_cache[cache_key]
                del self._memory_ttl[cache_key]
        
        # 再查磁盘缓存
        if self._disk_enabled:
            disk_path = os.path.join(self._disk_path, cache_key)
            if os.path.exists(disk_path):
                try:
                    mtime = os.path.getmtime(disk_path)
                    if now - mtime < self._memory_ttl:
                        with open(disk_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            self._stats.hits += 1
                            # 同时写入内存缓存
                            self.set(key, data)
                            return data
                except Exception:
                    pass
        
        # 最后查Redis缓存
        if self._redis_enabled and self._redis_client:
            try:
                data = self._redis_client.get(cache_key)
                if data:
                    self._stats.hits += 1
                    result = json.loads(data)
                    self.set(key, result)
                    return result
            except Exception as exc:
                logger.warning("Redis get failed: %s", exc)
        
        self._stats.misses += 1
        return None

    def set(self, key: str, value: Any, ttl: int = None):
        """设置缓存值。"""
        cache_key = self._compute_key(key)
        ttl = ttl or self._memory_ttl
        expire_time = time.time() + ttl
        
        # 写入内存缓存
        self._memory_cleanup()
        self._memory_cache[cache_key] = value
        self._memory_ttl[cache_key] = expire_time
        self._stats.size = len(self._memory_cache)
        
        # 写入磁盘缓存
        if self._disk_enabled:
            try:
                disk_path = os.path.join(self._disk_path, cache_key)
                with open(disk_path, 'w', encoding='utf-8') as f:
                    json.dump(value, f)
            except Exception as exc:
                logger.warning("Disk cache write failed: %s", exc)
        
        # 写入Redis缓存
        if self._redis_enabled and self._redis_client:
            try:
                self._redis_client.set(cache_key, json.dumps(value), ex=ttl)
            except Exception as exc:
                logger.warning("Redis set failed: %s", exc)

    def delete(self, key: str):
        """删除缓存。"""
        cache_key = self._compute_key(key)
        
        if cache_key in self._memory_cache:
            del self._memory_cache[cache_key]
            del self._memory_ttl[cache_key]
        
        if self._disk_enabled:
            disk_path = os.path.join(self._disk_path, cache_key)
            if os.path.exists(disk_path):
                os.remove(disk_path)
        
        if self._redis_enabled and self._redis_client:
            try:
                self._redis_client.delete(cache_key)
            except Exception as exc:
                logger.warning("Redis delete failed: %s", exc)

    def clear(self):
        """清空所有缓存。"""
        self._memory_cache.clear()
        self._memory_ttl.clear()
        self._stats = CacheStats(max_size=self._memory_max_size)
        
        if self._disk_enabled:
            for f in os.listdir(self._disk_path):
                os.remove(os.path.join(self._disk_path, f))
        
        if self._redis_enabled and self._redis_client:
            try:
                self._redis_client.flushdb()
            except Exception as exc:
                logger.warning("Redis flush failed: %s", exc)

    def get_stats(self) -> CacheStats:
        """获取缓存统计信息。"""
        return self._stats


class ConnectionPool:
    """连接池管理器 — 管理HTTP客户端、数据库连接等资源。"""

    def __init__(self, config: Dict[str, Any] = None):
        config = config or {}
        self._http_clients: Dict[str, Any] = {}
        self._db_connections: Dict[str, Any] = {}
        self._max_http_clients = config.get("max_http_clients", 10)
        self._max_db_connections = config.get("max_db_connections", 5)
        
        self._lock = asyncio.Lock()

    async def get_http_client(self, base_url: str):
        """获取或创建HTTP客户端。"""
        async with self._lock:
            if base_url in self._http_clients:
                return self._http_clients[base_url]
            
            # 限制最大连接数
            if len(self._http_clients) >= self._max_http_clients:
                # 删除最早的客户端
                oldest = min(self._http_clients.keys(), key=lambda k: id(self._http_clients[k]))
                del self._http_clients[oldest]
            
            import httpx
            client = httpx.AsyncClient(
                base_url=base_url,
                timeout=30,
                limits=httpx.Limits(max_connections=10)
            )
            self._http_clients[base_url] = client
            return client

    async def close(self):
        """关闭所有连接。"""
        async with self._lock:
            for client in self._http_clients.values():
                try:
                    await client.aclose()
                except Exception:
                    pass
            self._http_clients.clear()


def async_lru_cache(maxsize: int = 128):
    """异步函数的LRU缓存装饰器。"""
    cache = {}
    cache_order = []
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            key = (args, frozenset(kwargs.items()))
            
            if key in cache:
                # 刷新访问顺序
                cache_order.remove(key)
                cache_order.append(key)
                return cache[key]
            
            # 检查缓存大小
            if len(cache) >= maxsize:
                oldest = cache_order.pop(0)
                del cache[oldest]
            
            result = await func(*args, **kwargs)
            cache[key] = result
            cache_order.append(key)
            return result
        
        return wrapper
    return decorator


@dataclass
class PerformanceMetrics:
    """性能指标收集类。"""
    requests: int = 0
    errors: int = 0
    total_time: float = 0.0
    avg_time: float = 0.0
    min_time: float = float('inf')
    max_time: float = 0.0


class PerformanceMonitor:
    """性能监控器 — 收集和分析性能指标。"""

    def __init__(self):
        self._metrics: Dict[str, PerformanceMetrics] = {}
        self._lock = asyncio.Lock()

    async def record(self, operation: str, duration: float, success: bool = True):
        """记录操作性能数据。"""
        async with self._lock:
            if operation not in self._metrics:
                self._metrics[operation] = PerformanceMetrics()
            
            metrics = self._metrics[operation]
            metrics.requests += 1
            if not success:
                metrics.errors += 1
            metrics.total_time += duration
            metrics.avg_time = metrics.total_time / metrics.requests
            metrics.min_time = min(metrics.min_time, duration)
            metrics.max_time = max(metrics.max_time, duration)

    def get_metrics(self, operation: str = None) -> Dict[str, Any]:
        """获取性能指标。"""
        if operation:
            metrics = self._metrics.get(operation)
            if metrics:
                return {
                    "operation": operation,
                    "requests": metrics.requests,
                    "errors": metrics.errors,
                    "avg_time": metrics.avg_time,
                    "min_time": metrics.min_time,
                    "max_time": metrics.max_time,
                    "error_rate": metrics.errors / metrics.requests if metrics.requests > 0 else 0.0
                }
            return {}
        
        result = {}
        for op, metrics in self._metrics.items():
            result[op] = {
                "requests": metrics.requests,
                "errors": metrics.errors,
                "avg_time": metrics.avg_time,
                "min_time": metrics.min_time,
                "max_time": metrics.max_time,
                "error_rate": metrics.errors / metrics.requests if metrics.requests > 0 else 0.0
            }
        return result

    def reset(self, operation: str = None):
        """重置性能指标。"""
        if operation:
            self._metrics[operation] = PerformanceMetrics()
        else:
            self._metrics.clear()


class PerformancePlugin(Plugin):
    """性能优化插件。"""

    name = "performance"

    def __init__(self):
        super().__init__()
        self._cache = None
        self._connection_pool = None
        self._monitor = PerformanceMonitor()

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("performance", {}) or {}
        
        self._cache = MultiLevelCache(cfg.get("cache", {}))
        self._connection_pool = ConnectionPool(cfg.get("connection_pool", {}))
        
        logger.info("Performance plugin configured")

    async def stop(self) -> None:
        if self._connection_pool:
            await self._connection_pool.close()
        await super().stop()

    def get_cache(self) -> MultiLevelCache:
        """获取缓存系统。"""
        return self._cache

    def get_connection_pool(self) -> ConnectionPool:
        """获取连接池。"""
        return self._connection_pool

    def get_monitor(self) -> PerformanceMonitor:
        """获取性能监控器。"""
        return self._monitor

    def cache_decorator(self, ttl: int = 3600):
        """缓存装饰器工厂。"""
        cache = self._cache
        
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                key = f"{func.__name__}:{args}:{kwargs}"
                cached = cache.get(key)
                if cached is not None:
                    return cached
                
                result = await func(*args, **kwargs)
                cache.set(key, result, ttl)
                return result
            
            return wrapper
        return decorator