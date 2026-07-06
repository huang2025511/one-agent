"""Exponential Backoff — 指数退避重试，配合 failure_recovery 使用。

Provides:
  - ExponentialBackoff: configurable backoff strategy
  - RetryableOperation: async operation with retry and backoff
  - With jitter to avoid thundering herd
  - Integration with CircuitBreaker for coordinated failure handling
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class BackoffConfig:
    """Configuration for exponential backoff."""
    base_delay: float = 1.0          # initial delay in seconds
    max_delay: float = 60.0          # max delay cap
    multiplier: float = 2.0          # exponential factor
    jitter: bool = True              # add random jitter
    max_retries: int = 3             # max retry attempts
    retryable_exceptions: tuple = (Exception,)  # which exceptions to retry


class ExponentialBackoff:
    """Exponential backoff with jitter.

    Delay = min(base_delay * multiplier^attempt, max_delay) + jitter

    Usage:
        backoff = ExponentialBackoff(BackoffConfig(base_delay=1.0, max_retries=3))
        result = await backoff.retry(lambda: call_llm(prompt))
    """

    def __init__(self, config: Optional[BackoffConfig] = None):
        self._config = config or BackoffConfig()

    def compute_delay(self, attempt: int) -> float:
        """Compute delay for the given attempt number (0-indexed)."""
        delay = self._config.base_delay * (self._config.multiplier ** attempt)
        delay = min(delay, self._config.max_delay)

        if self._config.jitter:
            # Add ±25% jitter
            jitter = delay * 0.25 * (random.random() * 2 - 1)
            delay += jitter

        return max(0.1, delay)

    async def retry(
        self,
        func: Callable[..., Any],
        *args,
        on_retry: Optional[Callable[[int, Exception], None]] = None,
        **kwargs,
    ) -> T:
        """Execute a function with retry and exponential backoff.

        Args:
            func: the async function to call
            on_retry: optional callback(attempt, exception) called before each retry
        Returns:
            The function's return value
        Raises:
            The last exception if all retries are exhausted
        """
        last_exc = None

        for attempt in range(self._config.max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                return result
            except self._config.retryable_exceptions as exc:
                last_exc = exc

                if attempt < self._config.max_retries:
                    delay = self.compute_delay(attempt)
                    logger.debug(
                        "backoff: attempt %d/%d failed, retrying in %.1fs: %s",
                        attempt + 1, self._config.max_retries + 1, delay, exc,
                    )
                    if on_retry:
                        try:
                            on_retry(attempt + 1, exc)
                        except Exception:
                            pass
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "backoff: all %d attempts failed: %s",
                        self._config.max_retries + 1, exc,
                    )

        raise last_exc  # type: ignore[misc]

    def retry_sync(
        self,
        func: Callable[..., Any],
        *args,
        on_retry: Optional[Callable[[int, Exception], None]] = None,
        **kwargs,
    ) -> T:
        """Synchronous version of retry()."""
        import time as _time

        last_exc = None

        for attempt in range(self._config.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                return result
            except self._config.retryable_exceptions as exc:
                last_exc = exc

                if attempt < self._config.max_retries:
                    delay = self.compute_delay(attempt)
                    logger.debug(
                        "backoff (sync): attempt %d/%d failed, retrying in %.1fs: %s",
                        attempt + 1, self._config.max_retries + 1, delay, exc,
                    )
                    if on_retry:
                        try:
                            on_retry(attempt + 1, exc)
                        except Exception:
                            pass
                    _time.sleep(delay)
                else:
                    logger.error(
                        "backoff (sync): all %d attempts failed: %s",
                        self._config.max_retries + 1, exc,
                    )

        raise last_exc  # type: ignore[misc]


# Pre-configured backoff strategies for common scenarios

def llm_backoff() -> ExponentialBackoff:
    """Backoff for LLM API calls (5s base, 5 retries, max 120s)."""
    return ExponentialBackoff(BackoffConfig(
        base_delay=5.0,
        max_delay=120.0,
        multiplier=2.0,
        max_retries=5,
        jitter=True,
    ))


def search_backoff() -> ExponentialBackoff:
    """Backoff for web search (1s base, 3 retries, max 10s)."""
    return ExponentialBackoff(BackoffConfig(
        base_delay=1.0,
        max_delay=10.0,
        multiplier=2.0,
        max_retries=3,
        jitter=True,
    ))


def db_backoff() -> ExponentialBackoff:
    """Backoff for database operations (0.5s base, 3 retries, max 5s)."""
    return ExponentialBackoff(BackoffConfig(
        base_delay=0.5,
        max_delay=5.0,
        multiplier=2.0,
        max_retries=3,
        jitter=True,
    ))


def tool_backoff() -> ExponentialBackoff:
    """Backoff for tool execution (0.3s base, 2 retries, max 3s)."""
    return ExponentialBackoff(BackoffConfig(
        base_delay=0.3,
        max_delay=3.0,
        multiplier=2.0,
        max_retries=2,
        jitter=True,
    ))