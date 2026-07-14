"""Health Check API — readiness and liveness probes for k8s/OPS.

Provides standardized health check endpoints:
- GET /health — Basic health check (always 200 if running)
- GET /health/ready — Readiness probe (checks dependencies)
- GET /health/live — Liveness probe (checks if process is alive)
- GET /health/detailed — Full diagnostics with component status

Kubernetes integration:
- Liveness: Is the process running? (lightweight, no deps)
- Readiness: Is it ready to serve traffic? (checks DB, LLM, etc.)
- Startup: One-time check during container startup
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db import create_sqlite_connection

logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    """Health check status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Some components failing but functional
    UNHEALTHY = "unhealthy"  # Critical components failing


@dataclass
class ComponentCheck:
    """Result of a single component health check."""
    name: str
    status: HealthStatus
    message: str = ""
    latency_ms: float = 0
    details: Dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = {}


class HealthChecker:
    """Performs health checks on all system components."""

    def __init__(self) -> None:
        self._checks: Dict[str, callable] = {}
        self._start_time = time.time()

    def register_check(self, name: str, check_fn: callable) -> None:
        """Register a health check function.

        The function should return a ComponentCheck.
        """
        self._checks[name] = check_fn
        logger.debug("Registered health check: %s", name)

    def check_all(self) -> Dict[str, Any]:
        """Run all health checks and return results."""
        results: Dict[str, ComponentCheck] = {}
        overall_status = HealthStatus.HEALTHY

        for name, check_fn in self._checks.items():
            start = time.time()
            try:
                result = check_fn()
                if not isinstance(result, ComponentCheck):
                    result = ComponentCheck(
                        name=name,
                        status=HealthStatus.HEALTHY,
                        message=str(result),
                    )
                result.latency_ms = (time.time() - start) * 1000
                results[name] = result

                # Update overall status
                if result.status == HealthStatus.UNHEALTHY:
                    overall_status = HealthStatus.UNHEALTHY
                elif result.status == HealthStatus.DEGRADED and overall_status != HealthStatus.UNHEALTHY:
                    overall_status = HealthStatus.DEGRADED

            except Exception as exc:
                results[name] = ComponentCheck(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Check failed: {exc}",
                    latency_ms=(time.time() - start) * 1000,
                )
                overall_status = HealthStatus.UNHEALTHY
                logger.warning("Health check %s failed: %s", name, exc)

        return {
            "status": overall_status.value,
            "uptime_seconds": time.time() - self._start_time,
            "timestamp": time.time(),
            "components": {
                name: {
                    "status": check.status.value,
                    "message": check.message,
                    "latency_ms": round(check.latency_ms, 2),
                    "details": check.details,
                }
                for name, check in results.items()
            },
        }

    def check_liveness(self) -> Dict[str, Any]:
        """Lightweight liveness check (is process alive?)."""
        return {
            "status": HealthStatus.HEALTHY.value,
            "timestamp": time.time(),
            "uptime_seconds": time.time() - self._start_time,
        }

    def check_readiness(self) -> Dict[str, Any]:
        """Full readiness check (can serve traffic?)."""
        return self.check_all()


# ===================================================== Built-in health checks

def _check_database(db_path: str = "data/memory/sessions.db") -> ComponentCheck:
    """Check if SQLite database is accessible."""
    try:
        if not Path(db_path).exists():
            return ComponentCheck(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message=f"Database not found: {db_path}",
            )

        conn = create_sqlite_connection(db_path, busy_timeout_ms=1000)
        cur = conn.execute("SELECT COUNT(*) FROM sessions")
        count = cur.fetchone()[0]
        conn.close()

        return ComponentCheck(
            name="database",
            status=HealthStatus.HEALTHY,
            message=f"Accessible, {count} sessions",
            details={"session_count": count},
        )
    except Exception as exc:
        return ComponentCheck(
            name="database",
            status=HealthStatus.UNHEALTHY,
            message=f"Database error: {exc}",
        )


def _check_memory() -> ComponentCheck:
    """Check if memory stores are accessible."""
    try:
        # Check if memory DB exists and has data
        kg_path = Path("data/memory/kg.db")
        emb_path = Path("data/memory/embeddings.db")

        kg_count = 0
        emb_count = 0

        if kg_path.exists():
            conn = create_sqlite_connection(str(kg_path), busy_timeout_ms=1000)
            cur = conn.execute("SELECT COUNT(*) FROM entities")
            kg_count = cur.fetchone()[0]
            conn.close()

        if emb_path.exists():
            conn = create_sqlite_connection(str(emb_path), busy_timeout_ms=1000)
            cur = conn.execute("SELECT COUNT(*) FROM embeddings")
            emb_count = cur.fetchone()[0]
            conn.close()

        return ComponentCheck(
            name="memory",
            status=HealthStatus.HEALTHY,
            message=f"Entities: {kg_count}, Embeddings: {emb_count}",
            details={"entities": kg_count, "embeddings": emb_count},
        )
    except Exception as exc:
        return ComponentCheck(
            name="memory",
            status=HealthStatus.DEGRADED,
            message=f"Memory check failed: {exc}",
        )


def _check_llm_provider() -> ComponentCheck:
    """Check if LLM provider is configured."""
    try:
        import os
        # Check for any configured API key
        key_vars = [
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
            "SENSENOVA_API_KEY", "DASHSCOPE_API_KEY",
        ]

        configured = [v for v in key_vars if os.environ.get(v)]
        if configured:
            return ComponentCheck(
                name="llm_provider",
                status=HealthStatus.HEALTHY,
                message=f"{len(configured)} provider(s) configured",
                details={"configured_providers": configured},
            )
        else:
            return ComponentCheck(
                name="llm_provider",
                status=HealthStatus.DEGRADED,
                message="No LLM API key configured",
                details={"configured_providers": []},
            )
    except Exception as exc:
        return ComponentCheck(
            name="llm_provider",
            status=HealthStatus.UNHEALTHY,
            message=f"LLM check failed: {exc}",
        )


def _check_config() -> ComponentCheck:
    """Check if configuration is loaded."""
    try:
        import os
        from pathlib import Path

        config_path = Path("config/default_config.yaml")
        if not config_path.exists():
            return ComponentCheck(
                name="config",
                status=HealthStatus.UNHEALTHY,
                message="Config file not found",
            )

        return ComponentCheck(
            name="config",
            status=HealthStatus.HEALTHY,
            message="Config loaded",
            details={"config_path": str(config_path)},
        )
    except Exception as exc:
        return ComponentCheck(
            name="config",
            status=HealthStatus.UNHEALTHY,
            message=f"Config error: {exc}",
        )


def _check_disk_space() -> ComponentCheck:
    """Check available disk space."""
    try:
        import shutil

        total, used, free = shutil.disk_usage("/")
        free_gb = free / (1024 ** 3)

        # Warning if less than 1GB free
        if free_gb < 1:
            status = HealthStatus.DEGRADED
            message = f"Low disk space: {free_gb:.1f}GB free"
        else:
            status = HealthStatus.HEALTHY
            message = f"Disk space OK: {free_gb:.1f}GB free"

        return ComponentCheck(
            name="disk_space",
            status=status,
            message=message,
            details={"free_gb": round(free_gb, 2), "used_percent": round(used / total * 100, 1)},
        )
    except Exception as exc:
        return ComponentCheck(
            name="disk_space",
            status=HealthStatus.UNHEALTHY,
            message=f"Disk check failed: {exc}",
        )


# ===================================================== Health check handler

_health_checker: Optional[HealthChecker] = None


def get_health_checker() -> HealthChecker:
    """Get the shared health checker with default checks registered."""
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
        _health_checker.register_check("database", _check_database)
        _health_checker.register_check("memory", _check_memory)
        _health_checker.register_check("llm_provider", _check_llm_provider)
        _health_checker.register_check("config", _check_config)
        _health_checker.register_check("disk_space", _check_disk_space)
    return _health_checker


def format_health_response(
    check_type: str = "detailed",
    include_latency: bool = True,
) -> tuple[str, int]:
    """Format health check response for HTTP.

    Returns (response_body, http_status_code).
    """
    checker = get_health_checker()

    if check_type == "live":
        result = checker.check_liveness()
        status = HealthStatus.HEALTHY.value
    elif check_type == "ready":
        result = checker.check_readiness()
        status = result.get("status", HealthStatus.HEALTHY.value)
    else:
        result = checker.check_all()
        status = result.get("status", HealthStatus.HEALTHY.value)

    # HTTP status codes
    if status == HealthStatus.UNHEALTHY.value:
        http_status = 503  # Service Unavailable
    elif status == HealthStatus.DEGRADED.value:
        http_status = 200  # Still serving, but degraded
    else:
        http_status = 200

    return result, http_status