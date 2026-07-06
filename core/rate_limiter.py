"""Token Bucket Rate Limiter for LLM API calls.

Prevents overwhelming API providers with concurrent requests.
Each provider gets its own token bucket, configurable by:
- rate: tokens per second (refill rate)
- burst: maximum bucket size (burst capacity)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Default: 10 calls per second, burst of 20
DEFAULT_RATE = 10.0
DEFAULT_BURST = 20


class TokenBucket:
    """Token bucket rate limiter for a single provider."""

    def __init__(self, rate: float = DEFAULT_RATE, burst: int = DEFAULT_BURST) -> None:
        self._rate = rate          # tokens per second
        self._burst = burst        # max tokens
        self._tokens = float(burst)  # current tokens
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._waiters = 0

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    async def acquire(self) -> bool:
        """Acquire one token. Blocks until available.

        修复：之前多 waiter 同时唤醒 (wait_time 几乎相同) 后无条件 self._tokens -= 1.0,
        导致 tokens 变负、限流被穿透 (配置 1 token/sec 可能同时放行 10 个)。
        修复：醒来后重新检查 token 是否足够, 不足则继续等待 (循环直到拿到)。
        """
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                # Not enough tokens — wait for refill
                wait_time = (1.0 - self._tokens) / self._rate
                self._waiters += 1

            # Release lock before sleeping
            await asyncio.sleep(wait_time)

            async with self._lock:
                self._waiters -= 1
                self._refill()
                if self._tokens >= 1.0:
                    # 醒来后 token 已足够, 扣除并返回
                    self._tokens -= 1.0
                    return True
                # token 仍不足 (其他 waiter 先抢到了) — 继续循环等待
                wait_time = (1.0 - self._tokens) / self._rate

    async def try_acquire(self) -> bool:
        """Try to acquire without blocking. Returns False if not available."""
        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    @property
    def available(self) -> float:
        """Current available tokens (approximate, no lock)."""
        elapsed = time.monotonic() - self._last_refill
        return min(self._burst, self._tokens + elapsed * self._rate)

    @property
    def waiters(self) -> int:
        return self._waiters


class LLMRateLimiter:
    """Per-provider rate limiter for LLM API calls.

    Usage:
        limiter = LLMRateLimiter()
        async with limiter.limit("openai"):
            result = await call_llm(...)
    """

    def __init__(self) -> None:
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def _get_bucket(self, provider: str) -> TokenBucket:
        """Get or create a bucket for the provider."""
        if provider not in self._buckets:
            async with self._lock:
                if provider not in self._buckets:
                    # Free/open-source providers get higher limits
                    if provider in ("ollama", "local"):
                        bucket = TokenBucket(rate=50.0, burst=100)
                    else:
                        bucket = TokenBucket(rate=DEFAULT_RATE, burst=DEFAULT_BURST)
                    self._buckets[provider] = bucket
        return self._buckets[provider]

    async def acquire(self, provider: str) -> None:
        """Acquire a token for the given provider. Blocks if needed."""
        bucket = await self._get_bucket(provider)
        await bucket.acquire()

    async def try_acquire(self, provider: str) -> bool:
        """Try to acquire without blocking."""
        bucket = await self._get_bucket(provider)
        return await bucket.try_acquire()

    def stats(self) -> Dict[str, Dict[str, float]]:
        """Get rate limiter statistics."""
        return {
            provider: {
                "available": bucket.available,
                "waiters": bucket.waiters,
            }
            for provider, bucket in self._buckets.items()
        }

    class LimitContext:
        """Async context manager for rate-limited operations."""
        def __init__(self, limiter: "LLMRateLimiter", provider: str) -> None:
            self._limiter = limiter
            self._provider = provider

        async def __aenter__(self):
            await self._limiter.acquire(self._provider)
            return self

        async def __aexit__(self, *args):
            pass

    def limit(self, provider: str) -> LimitContext:
        """Return an async context manager that acquires a token."""
        return self.LimitContext(self, provider)


# Singleton instance
_rate_limiter: Optional[LLMRateLimiter] = None


def get_rate_limiter() -> LLMRateLimiter:
    """Get the shared rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = LLMRateLimiter()
    return _rate_limiter