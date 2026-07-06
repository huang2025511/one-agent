"""Circuit Breaker — 全局熔断降级，防止级联故障。

Provides:
  - Per-service circuit breaker with 3 states: CLOSED → OPEN → HALF_OPEN
  - Configurable thresholds (failure count, timeout, success threshold)
  - Graceful degradation: when a service is OPEN, return fallback/default
  - Global circuit breaker manager for all external services
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"        # normal operation
    OPEN = "open"            # failing, reject requests
    HALF_OPEN = "half_open"  # testing if service recovered


@dataclass
class CircuitConfig:
    """Configuration for a circuit breaker."""
    failure_threshold: int = 5          # consecutive failures to trip OPEN
    success_threshold: int = 2          # successes in HALF_OPEN to reset to CLOSED
    timeout_seconds: float = 30.0       # time before HALF_OPEN after OPEN
    request_timeout_seconds: float = 10.0  # max time for a single request
    half_open_max_requests: int = 1     # max requests allowed in HALF_OPEN


@dataclass
class CircuitStats:
    """Statistics for a circuit breaker."""
    name: str = ""
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    successes: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    total_requests: int = 0
    total_failures: int = 0
    open_since: float = 0.0


class CircuitBreaker:
    """Per-service circuit breaker.

    States:
      CLOSED → (failures >= threshold) → OPEN
      OPEN → (timeout elapsed) → HALF_OPEN
      HALF_OPEN → (successes >= threshold) → CLOSED
      HALF_OPEN → (any failure) → OPEN
    """

    def __init__(self, name: str, config: Optional[CircuitConfig] = None):
        self._name = name
        self._config = config or CircuitConfig()
        self._state = CircuitState.CLOSED
        self._lock = threading.Lock()
        self._failures = 0
        self._successes = 0
        self._last_failure_time = 0.0
        self._last_success_time = 0.0
        self._open_since = 0.0
        self._total_requests = 0
        self._total_failures = 0
        self._fallback: Optional[Callable] = None

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition()
            return self._state

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    def set_fallback(self, fallback: Callable) -> None:
        """Set a fallback function to call when circuit is OPEN."""
        self._fallback = fallback

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute a function through the circuit breaker.

        If circuit is OPEN, raises CircuitOpenError or returns fallback.
        """
        with self._lock:
            self._maybe_transition()

            if self._state == CircuitState.OPEN:
                if self._fallback:
                    logger.debug("circuit %s is OPEN, using fallback", self._name)
                    return self._fallback()
                raise CircuitOpenError(
                    f"Circuit '{self._name}' is OPEN. "
                    f"Last failure: {time.time() - self._last_failure_time:.0f}s ago"
                )

            self._total_requests += 1

        try:
            result = func(*args, **kwargs)
            with self._lock:
                self._successes += 1
                self._last_success_time = time.time()
                # Check if we should transition to CLOSED
                self._maybe_transition()
            return result
        except Exception as exc:
            with self._lock:
                self._failures += 1
                self._total_failures += 1
                self._last_failure_time = time.time()
                self._maybe_transition()
            logger.warning(
                "circuit %s request failed (failures=%d/%d): %s",
                self._name, self._failures, self._config.failure_threshold, exc,
            )
            raise

    async def acall(self, func, *args, **kwargs) -> Any:
        """Async version of call().

        性能/并发修复：之前在 `with self._lock:` 块内 `await self._fallback()`,
        threading.Lock 是线程级阻塞原语, 持有期间 await 会让事件循环线程
        本身被卡住 (整个 loop 冻结, 所有 IO/定时器/其他协程停摆)。
        修复：锁内只读状态 + 决策, 锁外执行 await。
        """
        # 锁内：只做状态检查与决策, 不 await
        need_fallback = False
        with self._lock:
            self._maybe_transition()

            if self._state == CircuitState.OPEN:
                if self._fallback:
                    logger.debug("circuit %s is OPEN, using async fallback", self._name)
                    need_fallback = True
                else:
                    raise CircuitOpenError(
                        f"Circuit '{self._name}' is OPEN"
                    )
            else:
                self._total_requests += 1

        # 锁外：执行 fallback (可能是协程)
        if need_fallback:
            result = self._fallback()
            import asyncio
            if asyncio.iscoroutine(result):
                return await result
            return result

        try:
            result = await func(*args, **kwargs)
            with self._lock:
                self._successes += 1
                self._last_success_time = time.time()
                self._maybe_transition()
            return result
        except Exception as exc:
            with self._lock:
                self._failures += 1
                self._total_failures += 1
                self._last_failure_time = time.time()
                self._maybe_transition()
            logger.warning(
                "circuit %s async request failed (failures=%d/%d): %s",
                self._name, self._failures, self._config.failure_threshold, exc,
            )
            raise

    def _maybe_transition(self) -> None:
        """Check and apply state transitions."""
        now = time.time()

        if self._state == CircuitState.CLOSED:
            if self._failures >= self._config.failure_threshold:
                self._state = CircuitState.OPEN
                self._open_since = now
                self._successes = 0
                logger.warning(
                    "circuit %s OPENED after %d failures",
                    self._name, self._failures,
                )

        elif self._state == CircuitState.OPEN:
            if now - self._open_since >= self._config.timeout_seconds:
                self._state = CircuitState.HALF_OPEN
                self._successes = 0
                logger.info(
                    "circuit %s → HALF_OPEN (testing recovery)",
                    self._name,
                )

        elif self._state == CircuitState.HALF_OPEN:
            if self._failures > 0:
                # Failed in HALF_OPEN → back to OPEN
                self._state = CircuitState.OPEN
                self._open_since = now
                self._successes = 0
                logger.warning(
                    "circuit %s → OPEN (failed in HALF_OPEN)",
                    self._name,
                )
            elif self._successes >= self._config.success_threshold:
                # Enough successes → CLOSED
                self._state = CircuitState.CLOSED
                self._failures = 0
                self._successes = 0
                logger.info(
                    "circuit %s → CLOSED (recovered with %d successes)",
                    self._name, self._successes,
                )

    def reset(self) -> None:
        """Force reset the circuit to CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._successes = 0
            logger.info("circuit %s reset to CLOSED", self._name)

    def get_stats(self) -> CircuitStats:
        with self._lock:
            return CircuitStats(
                name=self._name,
                state=self._state,
                failures=self._failures,
                successes=self._successes,
                last_failure_time=self._last_failure_time,
                last_success_time=self._last_success_time,
                total_requests=self._total_requests,
                total_failures=self._total_failures,
                open_since=self._open_since,
            )


class CircuitOpenError(Exception):
    """Raised when a circuit is OPEN and no fallback is available."""
    pass


class CircuitManager:
    """Global circuit breaker manager.

    Manages circuit breakers for all external services (LLM providers,
    web search, database, etc.) and provides a unified interface.
    """

    def __init__(self):
        self._circuits: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, name: str, config: Optional[CircuitConfig] = None) -> CircuitBreaker:
        """Get or create a circuit breaker for a service."""
        with self._lock:
            if name not in self._circuits:
                self._circuits[name] = CircuitBreaker(name, config)
            return self._circuits[name]

    def list_circuits(self) -> List[CircuitStats]:
        """Get stats for all circuits."""
        with self._lock:
            return [cb.get_stats() for cb in self._circuits.values()]

    def reset_all(self) -> None:
        """Reset all circuits."""
        with self._lock:
            for cb in self._circuits.values():
                cb.reset()

    def get_dashboard_data(self) -> Dict[str, Any]:
        """Get circuit states for the monitoring dashboard."""
        stats = self.list_circuits()
        return {
            "total": len(stats),
            "open": sum(1 for s in stats if s.state == CircuitState.OPEN),
            "half_open": sum(1 for s in stats if s.state == CircuitState.HALF_OPEN),
            "closed": sum(1 for s in stats if s.state == CircuitState.CLOSED),
            "circuits": [
                {
                    "name": s.name,
                    "state": s.state.value,
                    "failures": s.failures,
                    "total_requests": s.total_requests,
                    "failure_rate": (
                        s.total_failures / s.total_requests
                        if s.total_requests > 0 else 0
                    ),
                }
                for s in stats
            ],
        }


# Singleton
_circuit_manager: Optional[CircuitManager] = None


def get_circuit_manager() -> CircuitManager:
    """Get the shared CircuitManager instance."""
    global _circuit_manager
    if _circuit_manager is None:
        _circuit_manager = CircuitManager()
    return _circuit_manager