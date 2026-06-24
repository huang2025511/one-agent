"""Failure Recovery — automatic strategy switching on failure.

When things go wrong, automatically try alternatives instead of failing:
- LLM provider failure → switch to fallback model
- Tool call failure → try alternative tool or answer from knowledge
- Rate limit / timeout → exponential backoff retry
- Empty / low-quality response → retry with different prompt

Tracks failure patterns to anticipate failures and switch strategies
proactively.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class FailureRecovery:
    """Manages failure recovery strategies.

    Strategies (tried in order):
    1. Retry with exponential backoff (for transient errors)
    2. Switch to fallback model (for provider/rate-limit errors)
    3. Simplify the request (reduce tokens, fewer tools)
    4. Degrade gracefully (return cached / default response)
    """

    def __init__(
        self,
        max_retries: int = 2,
        base_delay: float = 0.5,
        max_delay: float = 10.0,
    ) -> None:
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay

        # Failure tracking
        self._failure_count: Dict[str, int] = {}  # key -> count
        self._last_failure: Dict[str, float] = {}  # key -> timestamp

        # Circuit breakers: disable a provider/tool after too many failures
        self._circuit_breakers: Dict[str, Dict[str, Any]] = {}

    # ============================================================== Retry logic

    async def with_retry(
        self,
        func: Callable,
        *args,
        operation_id: str = "default",
        max_retries: Optional[int] = None,
        retry_on_exceptions: Tuple = (Exception,),
        **kwargs,
    ) -> Any:
        """Execute an async function with retry and backoff.

        Args:
            func: Async function to call
            operation_id: Identifier for tracking failure rates
            max_retries: Override default max retries
            retry_on_exceptions: Exception types to retry on

        Returns:
            Function result

        Raises:
            Last exception if all retries fail
        """
        max_retries = max_retries if max_retries is not None else self._max_retries
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                # Success — reset failure count for this operation
                self._record_success(operation_id)
                return result
            except retry_on_exceptions as exc:
                last_exception = exc
                self._record_failure(operation_id, exc)

                if attempt < max_retries:
                    delay = self._calculate_backoff(attempt)
                    logger.warning(
                        "Operation %s failed (attempt %d/%d), retrying in %.1fs: %s",
                        operation_id,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Operation %s failed after %d attempts: %s",
                        operation_id,
                        max_retries + 1,
                        exc,
                    )

        # All retries exhausted
        raise last_exception  # type: ignore[misc]

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff delay."""
        delay = self._base_delay * (2 ** attempt)
        return min(delay, self._max_delay)

    # ============================================================== Circuit breaker

    def is_circuit_open(self, key: str) -> bool:
        """Check if a circuit breaker is open (operation should be skipped)."""
        breaker = self._circuit_breakers.get(key)
        if not breaker:
            return False

        # Check if cooldown period has passed
        now = time.time()
        if now - breaker["last_failure"] > breaker["cooldown"]:
            # Half-open: allow one attempt to test recovery
            breaker["state"] = "half-open"
            return False

        return breaker["state"] == "open"

    def _record_failure(self, key: str, error: Exception) -> None:
        """Record a failure and potentially trip the circuit breaker."""
        self._failure_count[key] = self._failure_count.get(key, 0) + 1
        self._last_failure[key] = time.time()

        # Update circuit breaker
        breaker = self._circuit_breakers.get(key)
        if breaker is None:
            breaker = {
                "failures": 0,
                "state": "closed",
                "last_failure": time.time(),
                "threshold": 5,  # Trip after 5 failures
                "cooldown": 60,  # Cooldown for 60 seconds
            }
            self._circuit_breakers[key] = breaker

        breaker["failures"] += 1
        breaker["last_failure"] = time.time()

        if breaker["state"] == "half-open":
            # Failed in half-open — re-open the circuit
            breaker["state"] = "open"
            breaker["failures"] = breaker["threshold"] + 1
            logger.warning("Circuit breaker re-opened for %s", key)
        elif breaker["failures"] >= breaker["threshold"] and breaker["state"] == "closed":
            # Trip the circuit breaker
            breaker["state"] = "open"
            logger.warning(
                "Circuit breaker opened for %s after %d failures",
                key,
                breaker["failures"],
            )

    def _record_success(self, key: str) -> None:
        """Record a success and potentially reset the circuit breaker."""
        self._failure_count.pop(key, None)
        self._last_failure.pop(key, None)

        breaker = self._circuit_breakers.get(key)
        if breaker and breaker["state"] == "half-open":
            # Success in half-open — close the circuit
            breaker["state"] = "closed"
            breaker["failures"] = 0
            logger.info("Circuit breaker closed for %s", key)

    # ============================================================== Model fallback

    def get_fallback_model(
        self,
        current_model: str,
        available_models: List[str],
        failure_key: str = "",
    ) -> Optional[str]:
        """Get a fallback model when the current one fails.

        Picks a model from the available list that:
        1. Isn't the current model
        2. Hasn't tripped its circuit breaker
        3. Has the lowest failure count
        """
        if not available_models:
            return None

        # Filter out current and broken models
        candidates = []
        for model in available_models:
            if model == current_model:
                continue
            model_key = f"model:{model}"
            if self.is_circuit_open(model_key):
                continue
            failures = self._failure_count.get(model_key, 0)
            candidates.append((model, failures))

        if not candidates:
            return None

        # Sort by failure count (least first)
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]

    # ============================================================== Failure pattern analysis

    def get_failure_stats(self) -> Dict[str, Any]:
        """Get failure statistics."""
        total_failures = sum(self._failure_count.values())
        open_circuits = sum(
            1 for b in self._circuit_breakers.values() if b["state"] == "open"
        )

        # Top failure points
        top_failures = sorted(
            self._failure_count.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:10]

        return {
            "total_failures": total_failures,
            "unique_failure_points": len(self._failure_count),
            "open_circuits": open_circuits,
            "total_circuit_breakers": len(self._circuit_breakers),
            "top_failures": top_failures,
        }

    def reset(self) -> None:
        """Reset all failure tracking."""
        self._failure_count.clear()
        self._last_failure.clear()
        self._circuit_breakers.clear()


# Singleton
_failure_recovery: Optional[FailureRecovery] = None


def get_failure_recovery() -> FailureRecovery:
    """Get the shared FailureRecovery instance."""
    global _failure_recovery
    if _failure_recovery is None:
        _failure_recovery = FailureRecovery()
    return _failure_recovery