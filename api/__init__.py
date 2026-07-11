"""REST API server — FastAPI backend for external integrations.

Exposes:
  POST /api/chat           — send a message, get a reply
  GET  /api/memory/search  — query long-term memory
  POST /api/memory/add     — add a fact to memory
  GET  /api/memory/page    — paginated memory list
  GET  /api/skills         — list all skills
  POST /api/skills/install — install a skill from URL / GitHub
  GET  /api/marketplace           — discover available skill packages
  POST /api/marketplace/publish   — publish a local skill to marketplace
  POST /api/marketplace/install   — install a skill from marketplace
  DELETE /api/marketplace/{name}  — uninstall a skill
  GET  /api/stats          — system statistics
  GET  /api/metrics        — bus + LLM + memory metrics
  POST /api/cache/clear    — clear LLM response cache
  GET  /api/health         — health check

All endpoints return JSON.  Authentication via X-API-Key header
(configurable via ONE_AGENT_API_KEY env var).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.exceptions import InputValidationError
from core.plugin import Plugin

# 修复 bug：文件有 `from __future__ import annotations`，所有类型注解会
# 变成字符串 ForwardRef。FastAPI 解析 `request: Request` 时需要在模块全局
# 命名空间找到 `Request`。但 Request 之前只在方法内部局部导入
# （`from fastapi import Request`），FastAPI 解析 ForwardRef('Request') 时
# 找不到 → chat 路由 422: loc=["query","request"] Field required。
# 在顶层导入 Request（及 Header/Body/HTTPException）让 ForwardRef 能解析。
# fastapi 是可选依赖，用 try/except 保护导入。
try:
    from fastapi import Body, Header, HTTPException, Request
except ImportError:  # pragma: no cover — fastapi 未装时的降级路径
    Body = Header = HTTPException = Request = None  # type: ignore[assignment]
from core.security import is_path_within
from i18n import _

logger = logging.getLogger(__name__)

# API configuration constants
# 默认绑定 127.0.0.1 而非 0.0.0.0 — 安全默认原则：
# 开发环境够用，生产环境需要用户显式配置为 0.0.0.0 或特定 IP
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18792
DEFAULT_RATE_LIMIT = 60
MAX_CHAT_BODY_SIZE = 64 * 1024  # 64 KB
MAX_CHAT_TEXT_LENGTH = 10000


def _validate_chat_text(text: str) -> str:
    """Validate chat text input: max 10000 chars, cannot be empty/whitespace."""
    if not isinstance(text, str):
        raise InputValidationError("Text must be a string")
    if not text.strip():
        raise InputValidationError("Text cannot be empty")
    if len(text) > MAX_CHAT_TEXT_LENGTH:
        raise InputValidationError(f"Text too long (max {MAX_CHAT_TEXT_LENGTH} characters)")
    return text


def _mask_api_key(key: str) -> str:
    """Mask API key for safe logging. Show only first 4 and last 4 chars."""
    if not key or len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


class RESTAPIGateway(Plugin):
    """FastAPI REST server plugin."""

    name = "gateway_rest"

    def __init__(self) -> None:
        super().__init__()
        self._host = DEFAULT_HOST
        self._port = DEFAULT_PORT
        self._enabled = True
        self._task = None
        self._app = None
        self._api_key = ""
        self._agent_callback = None
        # Per-IP rate-limit buckets — must live on the instance so they
        # survive any restart of the underlying FastAPI app (e.g. dev
        # mode auto-reload).  Format: {ip: [timestamp, ...]}
        self._rate_buckets: Dict[str, list] = {}
        self._last_bucket_cleanup: float = 0.0
        # Default rate limit (overridden in setup() from config)
        self._rate_limit = DEFAULT_RATE_LIMIT
        # Audit log for tracking operations
        self._audit_log = None
        # Max accepted chat request body size (bytes) — protects the
        # server from a single client streaming gigabytes of input.
        self._max_chat_bytes = MAX_CHAT_BODY_SIZE
        # CORS: default to localhost only for security; setup() reads config to
        # allow additional origins for production deployments.
        self._cors_origins: List[str] = ["http://localhost", "http://127.0.0.1"]
        # Trusted proxies for X-Forwarded-For (empty = don't trust any proxy)
        self._trusted_proxies: set = set()

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("rest") or {}
        self._host = cfg.get("host", self._host)
        self._port = int(cfg.get("port", self._port))
        self._enabled = bool(cfg.get("enabled", True))
        self._api_key = cfg.get("api_key", "")
        self._rate_limit = int(cfg.get("rate_limit_per_minute", self._rate_limit))
        self._max_chat_bytes = int(cfg.get("max_chat_bytes", self._max_chat_bytes))
        self._api_key = os.environ.get("ONE_AGENT_API_KEY", self._api_key)
        # CORS: restrict to configured origins in production.  Falls back
        # to a wildcard when no origins are configured (developer mode).
        self._cors_origins = cfg.get("cors_origins") or ["http://localhost", "http://127.0.0.1"]
        # Trusted proxies for X-Forwarded-For header validation
        # Only trust these IPs when extracting real client IP from X-Forwarded-For
        self._trusted_proxies = set(cfg.get("trusted_proxies", []))

        # ===== 安全检查 =====
        is_localhost = self._host in ("127.0.0.1", "localhost", "::1")

        # 检查 1: 非 localhost 绑定但无 API Key → 严重安全风险
        if not is_localhost and not self._api_key:
            logger.critical(
                "REST API 安全警告: 绑定到 %s 但未配置 api_key！\n"
                "  任何人都可以访问你的 API 并执行任意操作。\n"
                "  请立即在配置中设置 rest.api_key，或绑定到 127.0.0.1。",
                self._host,
            )

        # 检查 2: CORS 允许所有源 + 非 localhost → 中等风险
        has_wildcard_cors = "*" in (self._cors_origins or [])
        if not is_localhost and has_wildcard_cors:
            logger.warning(
                "REST API 安全提示: CORS 允许所有源 ('*') 且 API 绑定到 %s。\n"
                "  这意味着任何网站都可以跨域调用你的 API。\n"
                "  建议设置具体的 cors_origins 列表。",
                self._host,
            )

        # 检查 3: localhost 但无 API Key → 低风险，提示一下
        if is_localhost and not self._api_key:
            logger.info(
                "REST API: 本地模式（%s），未设置 api_key（仅本地可访问）",
                self._host,
            )

        # Initialize audit log
        from core.audit_log import AuditLog
        self._audit_log = AuditLog()

        logger.info("REST API configured on %s:%s auth=%s rate_limit=%d/min max_chat=%dB cors=%s trusted_proxies=%s",
                    self._host, self._port, _mask_api_key(self._api_key),
                    self._rate_limit, self._max_chat_bytes, self._cors_origins, self._trusted_proxies)

    def _setup_from_config(self, config) -> None:
        """Re-apply REST API settings from a reloaded config.

        Called by the /api/config/reload endpoint to update rate limits,
        CORS origins, trusted proxies, etc. without restarting the server.
        """
        cfg = (config.model_dump() if hasattr(config, "model_dump") else config).get("rest") or {}
        self._host = cfg.get("host", self._host)
        self._port = int(cfg.get("port", self._port))
        self._api_key = cfg.get("api_key", self._api_key)
        # Environment variable takes precedence over config file, matching
        # the setup() behavior. Without this, reloading config would silently
        # disable auth when the API key was set via env var.
        env_key = os.environ.get("ONE_AGENT_API_KEY")
        if env_key:
            self._api_key = env_key
        self._rate_limit = int(cfg.get("rate_limit_per_minute", self._rate_limit))
        self._max_chat_bytes = int(cfg.get("max_chat_bytes", self._max_chat_bytes))
        self._cors_origins = cfg.get("cors_origins") or self._cors_origins
        self._trusted_proxies = set(cfg.get("trusted_proxies", []))
        logger.info("REST API reconfigured: rate_limit=%d/min cors=%s trusted_proxies=%s",
                    self._rate_limit, self._cors_origins, self._trusted_proxies)

    def bind_callback(self, cb) -> None:
        self._agent_callback = cb
        # Store the app instance for thinking access
        if hasattr(cb, "__self__"):
            self._app_instance = cb.__self__

    def _get_plugin(self, name):
        """Get a plugin/attribute from the agent context by name.

        This is the class-level equivalent of the ``_cp`` closure historically
        defined inside ``start()``.  The ``_register_*`` helper methods build
        route handlers that need to look up plugins (session_store,
        marketplace, approval_manager, mcp_client, …) at request time.  Those
        handlers previously referenced a bare ``_cp`` name that only existed as
        a local in ``start()`` and was therefore *not* visible inside the
        separate ``_register_*`` methods — resulting in ``NameError`` at
        runtime and HTTP 500 on every endpoint that touched a context plugin
        (``/api/stats``, ``/api/marketplace``, ``/api/approvals/pending`` …).
        Each ``_register_*`` method now aliases this via ``_cp = self._get_plugin``.
        """
        ctx = self.ctx
        return getattr(ctx, name, None) if ctx else None

    def _register_health_routes(self, app, _ctx, _llm, _memory, _bus, _skills, _app_instance, _memory_plugin):
        _cp = self._get_plugin
        @app.get("/health")
        async def health_check():
            """Basic health check - service is alive"""
            return {
                "status": "healthy",
                "timestamp": time.time(),
                "version": "2.0.0"
            }

        @app.get("/ready")
        async def readiness_check():
            """Readiness check - service can handle requests"""
            def _check_database():
                """Check database connectivity."""
                try:
                    _session_store = _cp("session_store")
                    if _session_store:
                        _session_store.get_session_count()
                        return True
                except sqlite3.Error as e:
                    logger.error("Database check failed: %s", e)
                except Exception as e:
                    logger.error("Database check failed with unexpected error: %s", e)
                return False

            checks = {
                "database": _check_database(),
                "llm_configured": bool(_ctx.config.get("llm", {}).get("api_keys", {}).get("sensenova")) if _ctx else False,
                "memory_available": _memory_plugin is not None,
            }
            all_ok = all(checks.values())
            return {
                "status": "ready" if all_ok else "not_ready",
                "checks": checks,
                "timestamp": time.time()
            }

        @app.get("/api/health")
        async def health():
            """Enhanced health check with subsystem status for K8s probes."""
            if _ctx is None:
                return {
                    "status": "not_ready",
                    "uptime": 0,
                    "timestamp": time.time(),
                    "version": "2.0.0",
                    "components": {}
                }

            uptime = int(time.time() - _ctx.started_at)
            components = {}

            # Database connectivity check
            try:
                _session_store = getattr(_ctx, "session_store", None)
                if _session_store:
                    _session_store.get_session_count()
                    components["database"] = {"status": "ok", "type": "sqlite"}
                else:
                    components["database"] = {"status": "unavailable"}
            except Exception as e:
                components["database"] = {"status": "error", "message": str(e)}

            # LLM provider connectivity and stats
            if _llm is not None:
                try:
                    llm_s = _llm.stats()
                    components["llm"] = {
                        "status": "ok" if not llm_s.get("failed") else "degraded",
                        "calls": llm_s.get("calls", 0),
                        "tokens_used": llm_s.get("tokens_used", 0),
                        "cache_hit_rate": llm_s.get("cache", {}).get("hit_rate", 0),
                        "provider": getattr(_llm, '_primary_provider', 'unknown')
                    }
                except Exception as e:
                    components["llm"] = {"status": "error", "message": str(e)}
            else:
                components["llm"] = {"status": "unavailable"}

            # Memory subsystem
            if _memory is not None:
                try:
                    mem_stats = _memory.stats() if hasattr(_memory, 'stats') else {}
                    components["memory"] = {
                        "status": "ok",
                        "long_term_rows": mem_stats.get("rows", 0)
                    }
                except Exception as e:
                    components["memory"] = {"status": "error", "message": str(e)}
            else:
                components["memory"] = {"status": "unavailable"}

            # Event bus health
            if _bus is not None:
                try:
                    bus_m = _bus.metrics()
                    components["bus"] = {
                        "status": "ok",
                        "queue_depth": bus_m.get("queue_depth", 0),
                        "errors": bus_m.get("errors", 0),
                        "published": bus_m.get("published", 0),
                        "processed": bus_m.get("processed", 0)
                    }
                except Exception as e:
                    components["bus"] = {"status": "error", "message": str(e)}
            else:
                components["bus"] = {"status": "unavailable"}

            # Skills subsystem
            if _skills is not None:
                try:
                    skill_ids = _skills.all_skill_ids()
                    components["skills"] = {
                        "status": "ok",
                        "count": len(skill_ids),
                        "sample": skill_ids[:5] if skill_ids else []
                    }
                except Exception as e:
                    components["skills"] = {"status": "error", "message": str(e)}
            else:
                components["skills"] = {"status": "unavailable"}

            # MCP client status
            if _app_instance and hasattr(_app_instance, 'mcp_client'):
                try:
                    mcp = _app_instance.mcp_client
                    tools = mcp.list_tools() if hasattr(mcp, 'list_tools') else []
                    components["mcp"] = {
                        "status": "ok",
                        "tools_count": len(tools)
                    }
                except Exception as e:
                    components["mcp"] = {"status": "error", "message": str(e)}
            else:
                components["mcp"] = {"status": "unavailable"}

            # Overall status: ok if critical components (database, llm, bus) are ok
            critical_ok = all(
                components.get(k, {}).get("status") in ("ok", "unavailable")
                for k in ["database", "llm", "bus"]
            )
            overall = "ok" if critical_ok else "degraded"

            return {
                "status": overall,
                "uptime": uptime,
                "timestamp": time.time(),
                "version": "2.0.0",
                "components": components,
            }

    def _register_dashboard_config_routes(self, app, auth, _ctx):
        from fastapi import Header, HTTPException
        @app.get("/dashboard")
        async def dashboard(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Serve the monitoring dashboard."""
            auth(x_api_key)
            from fastapi.responses import HTMLResponse

            from api.dashboard import get_dashboard_html
            return HTMLResponse(content=get_dashboard_html())

        # ── Configuration Management ───────────────────────────────────
        @app.post("/api/config/reload")
        async def reload_config(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Hot reload configuration without restarting the service.

            Reloads the config file and updates runtime settings for:
            - LLM provider settings
            - Rate limits
            - CORS origins
            - Trusted proxies
            """
            auth(x_api_key)

            if _ctx is None:
                raise HTTPException(503, "Context not initialized")

            try:
                # Reload configuration from file
                import os

                from one_agent import load_config

                config_path = os.environ.get(
                    "ONE_AGENT_CONFIG",
                    str(Path(__file__).resolve().parent.parent / "config" / "default_config.yaml"),
                )
                new_config = load_config(config_path)

                # Update context config — must be a dict to match startup type
                # (one_agent.py sets _ctx.config = config.model_dump()). Pydantic
                # BaseModel has no .get(), so assigning the model object directly
                # would crash every downstream ctx.config.get(...) call.
                _ctx.config = new_config.model_dump() if hasattr(new_config, "model_dump") else new_config

                # Update API-specific settings
                if hasattr(self, '_setup_from_config'):
                    self._setup_from_config(new_config)

                logger.info("Configuration reloaded successfully")

                return {
                    "status": "ok",
                    "message": "Configuration reloaded",
                    "timestamp": time.time()
                }
            except Exception as e:
                logger.error("Failed to reload config: %s", e, exc_info=True)
                raise HTTPException(500, f"Config reload failed: {str(e)}")

        @app.get("/api/config")
        async def get_config(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get current runtime configuration (sanitized)."""
            auth(x_api_key)

            if _ctx is None:
                raise HTTPException(503, "Context not initialized")

            # Deep-copy the config so sanitization doesn't mutate the
            # live runtime config. A shallow copy shares nested dict
            # references, so modifying config["llm"]["api_keys"] would
            # overwrite the real API keys with "***" permanently.
            import copy
            config = copy.deepcopy(_ctx.config) if _ctx.config else {}

            # Mask sensitive fields
            if "llm" in config and "api_keys" in config["llm"]:
                config["llm"]["api_keys"] = {
                    k: "***" if v else None
                    for k, v in config["llm"]["api_keys"].items()
                }

            return {
                "config": config,
                "timestamp": time.time()
            }

    def _register_session_probe_routes(self, app, auth, _ctx, _agent):
        _cp = self._get_plugin
        from fastapi import Header, HTTPException
        @app.get("/api/sessions/list")
        async def sessions_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List recent sessions."""
            auth(x_api_key)
            _session_store = _cp("session_store")
            if _session_store is None:
                return {"sessions": []}
            sessions = _session_store.list_sessions(limit=20)
            return {"sessions": sessions}

        @app.post("/api/sessions/{session_id}/fork")
        async def fork_session(session_id: str, body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Fork a session at a specific message index."""
            auth(x_api_key)
            _session_store = _cp("session_store")
            if _session_store is None:
                raise HTTPException(503, _("session_store_not_available"))
            fork_point = body.get("fork_point", 0)
            new_id = body.get("new_session_id")
            result = _session_store.fork_session(session_id, fork_point, new_id)
            if result is None:
                raise HTTPException(400, _("fork_failed"))
            return {"new_session_id": result, "fork_point": fork_point}

        @app.get("/api/sessions/{session_id}/tree")
        async def session_tree(session_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get the fork tree of a session."""
            auth(x_api_key)
            _session_store = _cp("session_store")
            if _session_store is None:
                raise HTTPException(503, _("session_store_not_available"))
            tree = _session_store.get_session_tree(session_id)
            if not tree:
                raise HTTPException(404, _("session_not_found"))
            return tree

        @app.get("/api/health/ready")
        async def readiness():
            """Kubernetes-style readiness probe — returns 503 if not ready."""
            if _ctx is None or _agent is None:
                raise HTTPException(503, _("not_ready"))
            return {"ready": True}

        @app.get("/api/health/live")
        async def liveness():
            """Kubernetes-style liveness probe."""
            return {"alive": True}

    def _register_stats_metrics_routes(self, app, auth, _ctx, _llm, _memory, _bus, _skills, _memory_plugin):
        _cp = self._get_plugin
        # 修复：新版 fastapi（0.139+）把 JSONResponse 从顶层命名空间移除，
        # 必须从 fastapi.responses 导入。旧代码 `from fastapi import JSONResponse`
        # 会在 import 时抛 ImportError，导致整个 REST API gateway start 失败。
        from fastapi.responses import JSONResponse
        from fastapi import Header
        from typing import Optional
        @app.get("/api/stats")
        async def stats(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """System statistics for dashboard."""
            # 修复 bug：auth() 成功时返回 None（falsy），之前用
            # `if not auth(x_api_key):` 检查会误判为未授权 → 总是 401。
            # 正确用法是直接调用 auth()（失败时它自己抛 HTTPException(401)），
            # 和其它路由一致。
            auth(x_api_key)
            _session_store = _cp("session_store")
            _memory_plugin = _ctx.get_plugin("memory") if _ctx else None

            # Get session statistics
            sessions_data = {}
            total_messages = 0  # initialize to avoid UnboundLocalError
            if _session_store:
                try:
                    all_sessions = _session_store.list_sessions(limit=1000)
                    active_count = sum(1 for s in all_sessions if s.get("status") == "active")
                    total_messages = sum(s.get("message_count", 0) for s in all_sessions)
                    sessions_data = {
                        "active": active_count,
                        "total": len(all_sessions)
                    }
                except Exception as exc:
                    logger.warning("stats query failed: %s", exc)
                    sessions_data = {"active": 0, "total": 0}

            # Get knowledge graph entity count
            kg_data = {}
            if _memory_plugin and hasattr(_memory_plugin, "_kg"):
                try:
                    kg = _memory_plugin._kg
                    entity_count = len(kg.entities) if hasattr(kg, "entities") else 0
                    kg_data = {"entities": entity_count}
                except Exception as exc:
                    logger.warning("stats query failed: %s", exc)
                    kg_data = {"entities": 0}

            return {
                "uptime_seconds": _ctx.uptime() if _ctx else 0,
                "bus_metrics": _bus.metrics() if _bus else {},
                "llm_stats": _llm.stats() if _llm else {},
                "memory_stats": _memory.stats() if _memory else {},
                "skills_count": len(_skills.all_skill_ids()) if _skills else 0,
                # Dashboard-specific fields
                "sessions": sessions_data,
                "messages": {"total": total_messages},
                "knowledge_graph": kg_data,
                "skills": {"installed": len(_skills.all_skill_ids()) if _skills else 0},
            }

        @app.get("/api/metrics")
        async def metrics(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            # 修复：同 stats 路由，auth() 成功返回 None，不能用 if not 检查。
            auth(x_api_key)
            return {
                "bus": _bus.metrics() if _bus else {},
                "llm": _llm.stats() if _llm else {},
                "memory": _memory.stats() if _memory else {},
            }

        # ── Prometheus Metrics Endpoint ────────────────────────────────
        @app.get("/metrics")
        async def prometheus_metrics(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Prometheus-compatible metrics endpoint.

            Returns metrics in Prometheus text format for scraping.
            """
            # 修复：同 stats 路由，auth() 成功返回 None，不能用 if not 检查。
            auth(x_api_key)
            lines = []

            # System metrics
            if _ctx:
                uptime = time.time() - _ctx.started_at
                lines.append("# HELP one_agent_uptime_seconds Time since agent started")
                lines.append("# TYPE one_agent_uptime_seconds gauge")
                lines.append(f"one_agent_uptime_seconds {uptime:.2f}")

            # LLM metrics
            if _llm:
                stats = _llm.stats()
                lines.append("# HELP one_agent_llm_calls_total Total LLM API calls")
                lines.append("# TYPE one_agent_llm_calls_total counter")
                lines.append(f"one_agent_llm_calls_total {stats.get('calls', 0)}")

                lines.append("# HELP one_agent_llm_tokens_total Total tokens used")
                lines.append("# TYPE one_agent_llm_tokens_total counter")
                lines.append(f"one_agent_llm_tokens_total {stats.get('tokens_used', 0)}")

                lines.append("# HELP one_agent_llm_errors_total Total LLM errors")
                lines.append("# TYPE one_agent_llm_errors_total counter")
                lines.append(f"one_agent_llm_errors_total {stats.get('failed', 0)}")

                cache_stats = stats.get('cache', {})
                lines.append("# HELP one_agent_cache_hits_total Cache hits")
                lines.append("# TYPE one_agent_cache_hits_total counter")
                lines.append(f"one_agent_cache_hits_total {cache_stats.get('hits', 0)}")

                lines.append("# HELP one_agent_cache_misses_total Cache misses")
                lines.append("# TYPE one_agent_cache_misses_total counter")
                lines.append(f"one_agent_cache_misses_total {cache_stats.get('misses', 0)}")

            # Event bus metrics
            if _bus:
                bus_m = _bus.metrics()
                lines.append("# HELP one_agent_events_published_total Total events published")
                lines.append("# TYPE one_agent_events_published_total counter")
                lines.append(f"one_agent_events_published_total {bus_m.get('published', 0)}")

                lines.append("# HELP one_agent_events_processed_total Total events processed")
                lines.append("# TYPE one_agent_events_processed_total counter")
                lines.append(f"one_agent_events_processed_total {bus_m.get('processed', 0)}")

                lines.append("# HELP one_agent_events_errors_total Total event errors")
                lines.append("# TYPE one_agent_events_errors_total counter")
                lines.append(f"one_agent_events_errors_total {bus_m.get('errors', 0)}")

            # Memory metrics
            if _memory:
                mem_stats = _memory.stats()
                lines.append("# HELP one_agent_memory_rows Long-term memory rows")
                lines.append("# TYPE one_agent_memory_rows gauge")
                lines.append(f"one_agent_memory_rows {mem_stats.get('rows', 0)}")

            # Skills metrics
            if _skills:
                lines.append("# HELP one_agent_skills_loaded Number of skills loaded")
                lines.append("# TYPE one_agent_skills_loaded gauge")
                lines.append(f"one_agent_skills_loaded {len(_skills.all_skill_ids())}")

            # Audit log metrics
            if self._audit_log:
                audit_stats = self._audit_log.stats()
                lines.append("# HELP one_agent_audit_entries_total Total audit log entries")
                lines.append("# TYPE one_agent_audit_entries_total counter")
                lines.append(f"one_agent_audit_entries_total {audit_stats.get('total_entries', 0)}")

            return "\n".join(lines) + "\n"

    def _register_audit_routes(self, app, auth):
        from fastapi import Header
        @app.get("/api/audit")
        async def audit_query(
            action: Optional[str] = None,
            actor: Optional[str] = None,
            limit: int = 100,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """Query audit log entries with optional filters."""
            auth(x_api_key)
            if not self._audit_log:
                return {"entries": [], "message": "Audit log not initialized"}
            entries = self._audit_log.query(action=action, actor=actor, limit=min(limit, 500))
            return {"entries": entries, "count": len(entries)}

        @app.get("/api/audit/stats")
        async def audit_stats(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get audit log statistics."""
            auth(x_api_key)
            if not self._audit_log:
                return {"error": {"code": 503, "message": "Audit log not initialized", "type": "service_unavailable"}}
            return self._audit_log.stats()

    def _register_chat_routes(self, app, auth, _agent, _app_instance, _llm):
        from fastapi import Header, HTTPException, Body, Request
        @app.post("/api/chat")
        async def chat(request: Request, body: dict = Body(...), x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            text = body.get("text") or body.get("message", "")
            session_id = body.get("session_id") or uuid.uuid4().hex[:12]
            logger.info("chat: received request, text=%r, session_id=%s, client=%s",
                        text[:80], session_id, request.client.host if request.client else "?")

            # Validate text input
            try:
                _validate_chat_text(text)
            except InputValidationError as exc:
                # Log failed attempt
                if self._audit_log:
                    self._audit_log.log(
                        action="api_call",
                        actor=_mask_api_key(x_api_key) if x_api_key else "anonymous",
                        resource="/api/chat",
                        details={"error": str(exc), "text_length": len(text) if text else 0},
                        ip_address=request.client.host if request.client else None,
                        status="failure"
                    )
                raise HTTPException(400, str(exc))

            # Log successful API call
            if self._audit_log:
                self._audit_log.log(
                    action="api_call",
                    actor=_mask_api_key(x_api_key) if x_api_key else "anonymous",
                    resource="/api/chat",
                    details={"session_id": session_id, "text_length": len(text) if text else 0},
                    ip_address=request.client.host if request.client else None,
                    status="success"
                )

            # Auto-detect language from user input — use thread-local to
            # avoid multi-tenant language contention (global _current_lang
            # is shared across all concurrent requests).
            if text:
                from i18n import detect_language, set_thread_language
                detected_lang = detect_language(text)
                set_thread_language(detected_lang)
                # Sanitize language value to prevent log injection
                safe_lang = str(detected_lang).replace('\n', '\\n').replace('\r', '\\r')
                logger.info("Auto-detected language: %s from API request", safe_lang)

            if _agent is None:
                raise HTTPException(503, _("agent_not_ready"))
            # Use chat_with_thinking to get thinking process alongside reply
            if _app_instance and hasattr(_app_instance, "chat_with_thinking"):
                result = await _app_instance.chat_with_thinking(text, source="api", session_id=session_id)
            else:
                reply = await _agent(text, source="api", session_id=session_id)
                result = {"reply": reply, "session_id": session_id, "thinking": ""}
            return result

        @app.post("/api/chat/stream")
        async def chat_stream(body: dict, request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            text = body.get("text") or body.get("message", "")
            session_id = body.get("session_id") or uuid.uuid4().hex[:12]
            logger.info("chat/stream: received request, text=%r, session_id=%s, client=%s",
                        text[:80], session_id, request.client.host if request.client else "?")

            # Security restrictions: limit parameters to prevent abuse
            model = body.get("model")
            temperature = body.get("temperature")
            if temperature is not None:
                temperature = max(0.0, min(2.0, float(temperature)))

            max_tokens = body.get("max_tokens")
            if max_tokens is not None:
                max_tokens = max(50, min(8192, int(max_tokens)))

            # Ignore tools parameter for security - stream endpoint should be simple
            tools = None

            # Validate text input
            try:
                _validate_chat_text(text)
            except InputValidationError as exc:
                raise HTTPException(400, str(exc))

            # Auto-detect language from user input (thread-local for isolation)
            if text:
                from i18n import detect_language, set_thread_language
                detected_lang = detect_language(text)
                set_thread_language(detected_lang)

            from fastapi.responses import StreamingResponse

            async def event_generator():
                yield f"data: {json.dumps({'status': 'thinking', 'session_id': session_id})}\n\n"

                try:
                    if _app_instance and hasattr(_app_instance, "chat_with_thinking"):
                        # Full Coordinator pipeline: memory, skills, routing, tools.
                        # 之前流式端点直接调 _llm.chat_completion_stream，绕过了
                        # Coordinator → 无记忆、无技能、无路由，回复质量极差。
                        # 现在统一走 chat_with_thinking，和非流式 /api/chat 一致。
                        result = await _app_instance.chat_with_thinking(
                            text, source="api", session_id=session_id
                        )
                        reply = result.get("reply", "")
                        thinking = result.get("thinking", "")
                        if thinking:
                            yield f"data: {json.dumps({'status': 'thinking', 'content': thinking, 'session_id': session_id})}\n\n"
                        if reply:
                            yield f"data: {json.dumps({'content': reply, 'session_id': session_id})}\n\n"
                    elif _llm is not None:
                        # Fallback: direct LLM streaming (no Coordinator)
                        msgs: List[Dict[str, Any]] = [{"role": "user", "content": text}]
                        if body.get("system"):
                            msgs.insert(0, {"role": "system", "content": body["system"]})
                        chunks_sent = 0
                        async for chunk in _llm.chat_completion_stream(
                            messages=msgs,
                            model=model,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            tools=tools,
                        ):
                            if chunks_sent % 10 == 0:
                                if await request.is_disconnected():
                                    break
                            # Translate delta → content for client compatibility.
                            # LLM 层产出 {"delta": "...", "done": false}，
                            # 但客户端只认 content/text 字段，不翻译会导致空回复。
                            if "delta" in chunk and "content" not in chunk:
                                chunk["content"] = chunk.pop("delta")
                            yield f"data: {json.dumps(chunk)}\n\n"
                            chunks_sent += 1
                    else:
                        yield f"data: {json.dumps({'error': 'LLM provider not available', 'session_id': session_id})}\n\n"
                except Exception as exc:
                    logger.error("stream chat error: %s", exc, exc_info=True)
                    yield f"data: {json.dumps({'error': str(exc), 'session_id': session_id})}\n\n"

                yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

    def _register_memory_skills_routes(self, app, auth, _memory, _skills):
        from fastapi import Header, HTTPException
        @app.get("/api/memory/search")
        async def memory_search(
            q: str,
            limit: int = 5,
            offset: int = 0,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            auth(x_api_key)
            if _memory is None:
                raise HTTPException(503, _("memory_not_available"))
            results = _memory.search_facts(q, limit=limit, offset=offset)
            return {"query": q, "results": results, "limit": limit, "offset": offset}

        @app.post("/api/memory/add")
        async def memory_add(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            if _memory is None:
                raise HTTPException(503, _("memory_not_available"))
            text = body.get("text", "")
            tags = body.get("tags", "")
            source = body.get("source", "api")
            _memory.add_fact(text, source=source, tags=tags)
            return {"added": True, "text": text[:100]}

        @app.get("/api/memory/page")
        async def memory_page(
            page: int = 1,
            page_size: int = 20,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            auth(x_api_key)
            if _memory is None:
                raise HTTPException(503, _("memory_not_available"))
            return _memory.paginate_facts(page=page, page_size=page_size)

        @app.get("/api/skills")
        async def skills_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            if _skills is None:
                raise HTTPException(503, _("skills_not_available"))
            return {"skills": _skills.all_skill_ids()}

    def _register_marketplace_routes(self, app, auth, _ctx, _skills, _llm):
        _cp = self._get_plugin
        from fastapi import Header, HTTPException
        @app.get("/api/marketplace")
        async def list_marketplace(query: str = "", x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Discover available skill packages in the marketplace."""
            auth(x_api_key)
            mp = _cp("marketplace")
            if mp is None:
                raise HTTPException(503, _("marketplace_not_available"))
            return {"packages": mp.discover(query)}

        @app.post("/api/marketplace/publish")
        async def publish_skill(dirpath: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Publish a local skill directory to the marketplace."""
            auth(x_api_key)
            mp = _cp("marketplace")
            if mp is None:
                raise HTTPException(503, _("marketplace_not_available"))

            # Security: validate path is within allowed directories
            from pathlib import Path

            # Get allowed base directories
            workspace_root = Path.cwd().resolve()
            skills_dir = workspace_root / "skills"

            # Resolve the provided path
            try:
                resolved_path = Path(dirpath).resolve()
            except Exception:
                raise HTTPException(400, "Invalid path")

            # Check if path is within allowed directories
            allowed = False
            for allowed_dir in [skills_dir, workspace_root / "data" / "skills"]:
                try:
                    resolved_path.relative_to(allowed_dir)
                    allowed = True
                    break
                except ValueError:
                    continue

            if not allowed:
                raise HTTPException(
                    403,
                    f"Path must be within skills directory. Allowed: {skills_dir}"
                )

            # Additional check: ensure path exists and is a directory
            if not resolved_path.exists() or not resolved_path.is_dir():
                raise HTTPException(400, "Path does not exist or is not a directory")

            pkg = mp.publish(str(resolved_path))
            if pkg is None:
                raise HTTPException(400, _("invalid_skill_package", path=dirpath))
            return {"published": True, "package": pkg.to_dict()}

        @app.post("/api/marketplace/install")
        async def install_skill(name: str, target_dir: str = "", x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Install a skill package from the marketplace."""
            auth(x_api_key)
            mp = _cp("marketplace")
            if mp is None:
                raise HTTPException(503, _("marketplace_not_available"))

            # Security: restrict target_dir to prevent path traversal
            if not target_dir:
                target_dir = os.path.join(_ctx.config.get("agent", {}).get("data_dir", "./data"), "skills", "marketplace")
            else:
                # Validate path is within allowed directory (strict containment
                # via Path.relative_to — startswith can be bypassed by sibling
                # dirs like /data/skills_evil).
                allowed_base = os.path.realpath(os.path.join(
                    _ctx.config.get("agent", {}).get("data_dir", "./data"), "skills"))
                if not is_path_within(target_dir, allowed_base):
                    raise HTTPException(403, "target_dir must be within skills directory")

            ok = mp.install(name, target_dir)
            if not ok:
                raise HTTPException(404, _("skill_not_found", name=name))
            # Reload skills after installation
            if _skills is not None:
                _skills._scan_directory(target_dir)
            return {"installed": True, "name": name, "target_dir": target_dir}

        @app.delete("/api/marketplace/{name}")
        async def uninstall_skill(name: str, target_dir: str = "", x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Uninstall a skill package from the target directory."""
            auth(x_api_key)
            mp = _cp("marketplace")
            if mp is None:
                raise HTTPException(503, _("marketplace_not_available"))

            # Security: restrict target_dir to prevent path traversal
            if not target_dir:
                target_dir = os.path.join(_ctx.config.get("agent", {}).get("data_dir", "./data"), "skills", "marketplace")
            else:
                # Validate path is within allowed directory.
                # Use Path.relative_to instead of str.startswith to
                # prevent "/data/skills_evil" bypassing "/data/skills".
                allowed_base = os.path.realpath(os.path.join(_ctx.config.get("agent", {}).get("data_dir", "./data"), "skills"))
                if not is_path_within(target_dir, allowed_base):
                    raise HTTPException(403, "target_dir must be within skills directory")

            ok = mp.uninstall(name, target_dir)
            if not ok:
                raise HTTPException(404, _("skill_not_found", name=name))
            return {"uninstalled": True, "name": name}

        @app.post("/api/cache/clear")
        async def cache_clear(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            if _llm is None:
                raise HTTPException(503, _("llm_not_available"))
            return _llm.clear_cache()

    def _register_improvement_routes(self, app, auth, _ctx):
        _cp = self._get_plugin
        from fastapi import Header, HTTPException
        @app.get("/api/improvements")
        async def get_improvements(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get self-improvement stats and patterns."""
            auth(x_api_key)
            improver = _cp("self_improver")
            if improver is None:
                raise HTTPException(503, _("improvement_not_available"))
            stats = improver.get_stats()
            improvements = improver.get_improvements()
            return {**stats, "applied_improvements": improvements}

        @app.get("/api/improvements/failures")
        async def get_failures(limit: int = 50, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get recent failure cases."""
            auth(x_api_key)
            improver = _cp("self_improver")
            if improver is None:
                raise HTTPException(503, _("improvement_not_available"))
            failures = improver.get_failures(limit=limit)
            return {"failures": failures, "limit": limit}

    def _register_cost_routes(self, app, auth, _llm):
        from fastapi import Header, HTTPException
        @app.get("/api/costs/daily")
        async def daily_costs(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get daily cost breakdown."""
            auth(x_api_key)
            if _llm is None or getattr(_llm, "_cost_tracker", None) is None:
                raise HTTPException(503, _("cost_tracking_not_available"))
            tracker = _llm._cost_tracker
            return {
                "daily": tracker.daily_cost(),
                "by_provider": tracker.by_provider(),
                "by_model": tracker.by_model(),
                "total_tokens": tracker.total_tokens(),
                "total_cost": tracker.total_cost(),
            }

        @app.get("/api/costs/monthly")
        async def monthly_costs(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get monthly cost breakdown."""
            auth(x_api_key)
            if _llm is None or getattr(_llm, "_cost_tracker", None) is None:
                raise HTTPException(503, _("cost_tracking_not_available"))
            tracker = _llm._cost_tracker
            return {
                "monthly": tracker.monthly_cost(),
                "by_provider": tracker.by_provider(),
                "by_model": tracker.by_model(),
                "total_tokens": tracker.total_tokens(),
                "total_cost": tracker.total_cost(),
            }

        @app.get("/api/costs/budget")
        async def budget_status(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get budget status."""
            auth(x_api_key)
            if _llm is None or getattr(_llm, "_cost_tracker", None) is None:
                raise HTTPException(503, _("cost_tracking_not_available"))
            tracker = _llm._cost_tracker
            return tracker.check_budget()

        @app.get("/api/costs/by-provider")
        async def costs_by_provider(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get costs grouped by provider."""
            auth(x_api_key)
            if _llm is None or getattr(_llm, "_cost_tracker", None) is None:
                raise HTTPException(503, _("cost_tracking_not_available"))
            tracker = _llm._cost_tracker
            return {
                "by_provider": tracker.by_provider(),
                "by_model": tracker.by_model(),
                "total_cost": tracker.total_cost(),
            }

        @app.get("/api/costs/recent")
        async def costs_recent(
            limit: int = 50,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """Get recent cost entries."""
            auth(x_api_key)
            if _llm is None or getattr(_llm, "_cost_tracker", None) is None:
                raise HTTPException(503, _("cost_tracking_not_available"))
            tracker = _llm._cost_tracker
            return {"recent": tracker.get_recent(limit=limit)}

    def _register_sessions_routes(self, app, auth, _ctx):
        from fastapi import Header, HTTPException
        @app.get("/api/sessions")
        async def list_sessions(
            limit: int = 50,
            offset: int = 0,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            auth(x_api_key)
            store = _ctx.session_store if _ctx else None
            if store is None:
                raise HTTPException(503, _("session_store_not_available"))
            sessions = store.list_sessions(limit=limit, offset=offset)
            return {"sessions": sessions, "limit": limit, "offset": offset}

        @app.get("/api/sessions/{session_id}")
        async def get_session(
            session_id: str,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            auth(x_api_key)
            store = _ctx.session_store if _ctx else None
            if store is None:
                raise HTTPException(503, _("session_store_not_available"))
            session = store.get_session(session_id)
            if session is None:
                raise HTTPException(404, _("session_not_found", session_id=session_id))
            return session

        @app.delete("/api/sessions/{session_id}")
        async def delete_session(
            session_id: str,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            auth(x_api_key)
            store = _ctx.session_store if _ctx else None
            if store is None:
                raise HTTPException(503, _("session_store_not_available"))
            deleted = store.delete_session(session_id)
            if not deleted:
                raise HTTPException(404, _("session_not_found", session_id=session_id))
            return {"deleted": True, "session_id": session_id}

    def _register_settings_routes(self, app, auth, _ctx, _get_backup_mgr):
        from fastapi import Header, HTTPException
        @app.get("/api/settings")
        async def settings_get(key: Optional[str] = None, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """读取配置项。不传 key 则返回所有可配置项列表。"""
            auth(x_api_key)
            from skills import (
                _SENSITIVE_KEYS,
                _SETTING_ALIASES,
                _get_nested,
                _is_sensitive_write_allowed,
            )
            sensitive_allowed = _is_sensitive_write_allowed(_ctx.config) if _ctx else False
            if key is None:
                items = []
                for alias, (path, _) in _SETTING_ALIASES.items():
                    if any(c.isascii() and c.isalpha() for c in alias) and any("\u4e00" <= c <= "\u9fff" for c in alias):
                        continue
                    val = _get_nested(_ctx.config, path, None) if _ctx else None
                    if any(sk in path for sk in _SENSITIVE_KEYS) and isinstance(val, str) and len(val) > 8:
                        if not sensitive_allowed:
                            val = val[:4] + "****"
                    items.append({"alias": alias, "path": path, "value": val})
                return {"settings": items}
            for alias, (path, _) in _SETTING_ALIASES.items():
                if alias == key or path == key:
                    val = _get_nested(_ctx.config, path, None) if _ctx else None
                    if any(sk in path for sk in _SENSITIVE_KEYS) and isinstance(val, str) and len(val) > 8:
                        if not sensitive_allowed:
                            val = val[:4] + "****"
                    return {"alias": alias, "path": path, "value": val}
            return {"error": {"code": 404, "message": _("unknown_key", key=key), "type": "not_found"}}

        @app.post("/api/settings")
        async def settings_set(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """修改配置项。body: {"key": "模型", "value": "gpt-4o"}"""
            auth(x_api_key)
            from skills import (
                _SENSITIVE_KEYS,
                _SETTING_ALIASES,
                _is_sensitive_write_allowed,
                _parse_value,
                _save_config,
                _set_nested,
            )
            key = body.get("key", "")
            value = body.get("value")
            if not key or value is None:
                raise HTTPException(400, _("need_key_and_value"))
            for alias, (path, vtype) in _SETTING_ALIASES.items():
                if alias == key or path == key:
                    if any(sk in path for sk in _SENSITIVE_KEYS):
                        if not _is_sensitive_write_allowed(_ctx.config if _ctx else {}):
                            raise HTTPException(403, _("sensitive_setting_protected", alias=alias))
                    parsed = _parse_value(str(value), vtype)
                    if parsed is None:
                        raise HTTPException(400, _("cannot_parse_value", type=vtype.__name__))
                    if _ctx:
                        # Create backup before changing config
                        backup_mgr = _get_backup_mgr()
                        backup_mgr.create_backup(reason="pre-change")
                        _set_nested(_ctx.config, path, parsed)
                        _save_config(_ctx.config)
                    return {"alias": alias, "path": path, "value": parsed, "saved": True}
            raise HTTPException(404, _("unknown_key", key=key))

    def _register_config_backup_routes(self, app, auth, _get_backup_mgr):
        from fastapi import Header, HTTPException
        @app.get("/api/config/backups")
        async def config_backups_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List all config backups."""
            auth(x_api_key)
            backup_mgr = _get_backup_mgr()
            return {"backups": backup_mgr.list_backups()}

        @app.post("/api/config/backup")
        async def config_backup_create(body: dict = None, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Create a config backup."""
            auth(x_api_key)
            backup_mgr = _get_backup_mgr()
            reason = (body or {}).get("reason", "manual")
            backup_name = backup_mgr.create_backup(reason=reason)
            if backup_name:
                return {"created": True, "filename": backup_name}
            raise HTTPException(500, _("backup_create_failed"))

        @app.post("/api/config/restore")
        async def config_restore(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Restore config from a backup."""
            auth(x_api_key)
            backup_mgr = _get_backup_mgr()
            backup_name = body.get("filename")  # None means most recent
            success = backup_mgr.restore_backup(backup_name)
            if success:
                return {"restored": True, "filename": backup_name or "latest"}
            raise HTTPException(500, _("restore_failed"))

        @app.get("/api/config/backups/{filename}")
        async def config_backup_get(filename: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get backup content."""
            auth(x_api_key)
            # Validate filename to prevent path traversal
            if not filename or '/' in filename or '\\' in filename or '..' in filename:
                raise HTTPException(400, "invalid_filename")
            backup_mgr = _get_backup_mgr()
            content = backup_mgr.get_backup_content(filename)
            if content is not None:
                return {"filename": filename, "content": content}
            raise HTTPException(404, _("backup_not_found", filename=filename))

        @app.delete("/api/config/backups/{filename}")
        async def config_backup_delete(filename: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Delete a config backup."""
            auth(x_api_key)
            # Validate filename to prevent path traversal
            if not filename or '/' in filename or '\\' in filename or '..' in filename:
                raise HTTPException(400, "invalid_filename")
            backup_mgr = _get_backup_mgr()
            success = backup_mgr.delete_backup(filename)
            if success:
                return {"deleted": True, "filename": filename}
            raise HTTPException(404, _("backup_not_found", filename=filename))

    def _register_document_routes(self, app, auth, _ctx):
        from fastapi import Header, HTTPException, UploadFile
        @app.post("/api/documents/ingest")
        async def ingest_document(file: UploadFile = None, path: str = None,
                                  x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            from skills import get_doc_store
            _doc_store = get_doc_store()

            if file is not None:
                import shutil
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
                    shutil.copyfileobj(file.file, tmp)
                    tmp_path = tmp.name
                try:
                    count = _doc_store.ingest_file(tmp_path)
                finally:
                    os.unlink(tmp_path)
                return {"ingested": True, "name": file.filename, "chunks": count}

            if path:
                # Security: restrict path to data/documents directory only
                # Use Path.relative_to() for strict containment (startswith can be bypassed
                # e.g. "/data/skills_evil" starts with "/data/skills")
                allowed_base = os.path.realpath(os.path.join(
                    _ctx.config.get("agent", {}).get("data_dir", "./data"), "documents"))
                path_real = Path(os.path.realpath(path))
                if not is_path_within(path_real, allowed_base):
                    raise HTTPException(403, "path must be within data/documents directory")

                # Also check file exists and is a regular file
                if not path_real.is_file():
                    raise HTTPException(404, "file not found")

                count = _doc_store.ingest_file(str(path_real))
                return {"ingested": True, "name": path_real.name, "chunks": count}

            raise HTTPException(400, _("need_file_or_path"))

        @app.get("/api/documents")
        async def list_documents(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            from skills import get_doc_store
            docs = get_doc_store().list_documents()
            return {"documents": docs}

        @app.get("/api/documents/search")
        async def search_documents(q: str, limit: int = 5,
                                   x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            from skills import get_doc_store
            results = get_doc_store().search(q, limit=limit)
            return {"query": q, "results": results, "limit": limit}

        @app.delete("/api/documents/{name}")
        async def delete_document(name: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            from skills import get_doc_store
            deleted = get_doc_store().delete_document(name)
            if not deleted:
                raise HTTPException(404, _("document_not_found", name=name))
            return {"deleted": True, "name": name}

    def _register_alerting_routes(self, app, auth, _ctx):
        _cp = self._get_plugin
        from fastapi import Header, HTTPException
        @app.get("/api/alerts/rules")
        async def alert_rules_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List all alert rules."""
            auth(x_api_key)
            _alert_mgr = _cp("_alert_manager")
            if _alert_mgr is None:
                return {"rules": []}
            return {"rules": _alert_mgr.list_rules()}

        @app.post("/api/alerts/rules")
        async def alert_rule_create(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Create or update an alert rule."""
            auth(x_api_key)
            _alert_mgr = _cp("_alert_manager")
            if _alert_mgr is None:
                raise HTTPException(503, _("alert_manager_not_available"))
            from alerting import AlertRule
            try:
                rule = AlertRule(
                    name=body["name"],
                    metric_path=body["metric_path"],
                    operator=body["operator"],
                    threshold=float(body["threshold"]),
                    severity=body.get("severity", "warning"),
                    cooldown_seconds=body.get("cooldown_seconds", 300),
                    enabled=body.get("enabled", True),
                    description=body.get("description", ""),
                )
                _alert_mgr.add_rule(rule)
                return {"created": True, "rule": body["name"]}
            except KeyError as exc:
                raise HTTPException(400, _("missing_field", field=str(exc)))

        @app.delete("/api/alerts/rules/{name}")
        async def alert_rule_delete(name: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Delete an alert rule."""
            auth(x_api_key)
            _alert_mgr = _cp("_alert_manager")
            if _alert_mgr is None:
                raise HTTPException(503, _("alert_manager_not_available"))
            _alert_mgr.remove_rule(name)
            return {"deleted": True, "rule": name}

        @app.get("/api/alerts/history")
        async def alert_history(limit: int = 50, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get recent alert events."""
            auth(x_api_key)
            _alert_mgr = _cp("_alert_manager")
            if _alert_mgr is None:
                return {"alerts": []}
            return {"alerts": _alert_mgr.list_history(limit=limit)}

    def _register_approval_routes(self, app, auth, _ctx):
        _cp = self._get_plugin
        from fastapi import Header, HTTPException
        @app.get("/api/approvals/pending")
        async def list_pending_approvals(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List pending approval requests."""
            auth(x_api_key)
            _approval_mgr = _cp("approval_manager")
            if _approval_mgr is None:
                return {"pending": []}
            return {"pending": _approval_mgr.get_pending()}

        @app.post("/api/approvals/{request_id}/approve")
        async def approve_request(request_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Approve a pending request."""
            auth(x_api_key)
            _approval_mgr = _cp("approval_manager")
            if _approval_mgr is None:
                raise HTTPException(503, _("approval_manager_not_available"))
            ok = _approval_mgr.approve(request_id)
            if not ok:
                raise HTTPException(404, _("approval_request_not_found", request_id=request_id))
            return {"approved": True, "request_id": request_id}

        @app.post("/api/approvals/{request_id}/deny")
        async def deny_request(request_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Deny a pending request."""
            auth(x_api_key)
            _approval_mgr = _cp("approval_manager")
            if _approval_mgr is None:
                raise HTTPException(503, _("approval_manager_not_available"))
            ok = _approval_mgr.deny(request_id)
            if not ok:
                raise HTTPException(404, _("approval_request_not_found", request_id=request_id))
            return {"approved": False, "request_id": request_id}

    def _register_mcp_routes(self, app, auth, _ctx):
        _cp = self._get_plugin
        from fastapi import Header, HTTPException
        @app.get("/api/mcp/tools")
        async def mcp_list_tools(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """列出所有可用的 MCP 工具"""
            auth(x_api_key)
            _mcp_client = _cp("mcp_client")
            if _mcp_client is None:
                raise HTTPException(503, _("mcp_client_not_available"))
            tools = _mcp_client.list_tools()
            return {"tools": tools}

        @app.post("/api/mcp/call")
        async def mcp_call_tool(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """调用 MCP 工具"""
            auth(x_api_key)
            _mcp_client = _cp("mcp_client")
            if _mcp_client is None:
                raise HTTPException(503, _("mcp_client_not_available"))
            server_name = body.get("server")
            tool_name = body.get("tool")
            arguments = body.get("arguments", {})
            if not server_name or not tool_name:
                raise HTTPException(400, _("missing_required_fields", fields="server, tool"))
            try:
                result = await _mcp_client.call_tool(server_name, tool_name, arguments)
                return {"result": result}
            except ValueError as exc:
                raise HTTPException(404, str(exc))
            except Exception as exc:
                logger.exception("MCP tool call failed")
                raise HTTPException(500, _("mcp_tool_call_failed", error=str(exc)))

        @app.post("/api/mcp/add-server")
        async def mcp_add_server(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """添加并连接 MCP 服务器"""
            auth(x_api_key)
            _mcp_client = _cp("mcp_client")
            if _mcp_client is None:
                raise HTTPException(503, _("mcp_client_not_available"))
            name = body.get("name")
            url = body.get("url")
            api_key = body.get("api_key")
            if not name or not url:
                raise HTTPException(400, _("missing_required_fields", fields="name, url"))
            success = await _mcp_client.add_server(name, url, api_key)
            if not success:
                raise HTTPException(400, _("mcp_server_connection_failed"))
            return {"success": True, "server": name}

        @app.delete("/api/mcp/servers/{server_name}")
        async def mcp_remove_server(server_name: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """移除 MCP 服务器"""
            auth(x_api_key)
            _mcp_client = _cp("mcp_client")
            if _mcp_client is None:
                raise HTTPException(503, _("mcp_client_not_available"))
            await _mcp_client.remove_server(server_name)
            return {"success": True, "server": server_name}

    async def start(self) -> None:
        if not self._enabled:
            return
        try:
            from fastapi import (  # noqa: F401
                Body,
                FastAPI,
                Header,
                HTTPException,
                Request,
                UploadFile,
            )
            from fastapi.middleware.cors import CORSMiddleware
            from fastapi.responses import JSONResponse
        except ImportError:
            logger.warning("fastapi not installed — REST API disabled")
            return

        app = FastAPI(
            title="One-Agent API",
            version="2.0.0",
            description="REST API for One-Agent integration",
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=self._cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

        @app.middleware("http")
        async def rate_limit_middleware(request, call_next):
            # Get real client IP from X-Forwarded-For header
            # Only trust X-Forwarded-For if request comes from a trusted proxy
            client_ip = None
            if request.client:
                client_ip = request.client.host
            elif "client" in request.scope:
                # Fallback to scope client info
                client_info = request.scope["client"]
                if client_info and len(client_info) > 0:
                    client_ip = client_info[0]

            if not client_ip:
                # All unknown clients share a single bucket — using a unique
                # ID per request (e.g. md5(time.time())) would give every
                # request its own empty bucket, making the rate limit a no-op
                # and leaking memory into _rate_buckets indefinitely.
                client_ip = "unknown"
                logger.warning("Rate limit: unable to determine client IP, using shared 'unknown' bucket")

            if client_ip in self._trusted_proxies:
                # Request from trusted proxy - use X-Forwarded-For
                forwarded_for = request.headers.get("X-Forwarded-For")
                if forwarded_for:
                    # X-Forwarded-For format: client, proxy1, proxy2
                    # Take the first IP (original client)
                    ip = forwarded_for.split(",")[0].strip()
                else:
                    ip = client_ip
            else:
                # Direct connection or untrusted proxy - use client IP directly
                ip = client_ip

            now = time.time()
            bucket = self._rate_buckets.setdefault(ip, [])
            # evict entries older than 60s
            bucket[:] = [t for t in bucket if now - t < 60]
            # Periodically clean up stale buckets to prevent unbounded growth
            if not bucket and now - self._last_bucket_cleanup > 300:  # every 5 min
                self._last_bucket_cleanup = now
                stale = [k for k, v in self._rate_buckets.items()
                         if not v or (now - v[-1] > 60)]
                for k in stale:
                    self._rate_buckets.pop(k, None)
            if len(bucket) >= self._rate_limit:
                return JSONResponse({"error": {"code": 429, "message": _("rate_limit_exceeded"), "type": "rate_limit"}}, status_code=429)
            bucket.append(now)
            return await call_next(request)

        # Reject chat requests with absurdly large bodies before FastAPI
        # even tries to parse JSON — protects the server from accidental
        # 100 MB /chat posts.
        @app.middleware("http")
        async def body_size_middleware(request, call_next):
            if request.url.path in ("/api/chat", "/api/chat/stream"):
                cl = request.headers.get("content-length")
                try:
                    if cl is not None and int(cl) > self._max_chat_bytes:
                        return JSONResponse(
                            {"error": {"code": 413, "message": _("request_body_too_large", size=cl, max=self._max_chat_bytes), "type": "request_too_large"}},
                            status_code=413,
                        )
                except ValueError:
                    pass
            return await call_next(request)

        _agent = self._agent_callback
        _app_instance: Any = getattr(self, "_app_instance", None)
        _ctx = self.ctx
        _llm = _ctx.get_plugin("llm") if _ctx else None
        _memory = _ctx.get_plugin("memory") if _ctx else None
        _skills = _ctx.get_plugin("skills") if _ctx else None
        _bus = _ctx.bus if _ctx else None

        def _cp(name):
            """Get plugin from context."""
            return getattr(_ctx, name, None) if _ctx else None

        def auth(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> None:
            if self._api_key and not hmac.compare_digest(x_api_key or "", self._api_key):
                raise HTTPException(401, _("invalid_api_key"))

        def _get_backup_mgr():
            """Shared ConfigBackupManager factory for the config-backup endpoints."""
            from config_backup import ConfigBackupManager
            cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
            return ConfigBackupManager(cfg_path)

        # ---------------------------------------------------------------- Health & Readiness
        _memory_plugin = _ctx.get_plugin("memory") if _ctx else None

        self._register_health_routes(app, _ctx, _llm, _memory, _bus, _skills, _app_instance, _memory_plugin)

        # ── Dashboard ────────────────────────────────────────────────────────────
        self._register_dashboard_config_routes(app, auth, _ctx)

        # ── Dashboard API endpoints ──────────────────────────────────────────────────
        self._register_session_probe_routes(app, auth, _ctx, _agent)

        # ---------------------------------------------------------------- Stats & Metrics
        self._register_stats_metrics_routes(app, auth, _ctx, _llm, _memory, _bus, _skills, _memory_plugin)

        # ── Audit Log Endpoints ────────────────────────────────────────────────────────
        self._register_audit_routes(app, auth)

        # ---------------------------------------------------------------- Chat
        self._register_chat_routes(app, auth, _agent, _app_instance, _llm)

        # ---------------------------------------------------------------- Memory & Skills
        self._register_memory_skills_routes(app, auth, _memory, _skills)

        # ---------------------------------------------------------------- Marketplace endpoints
        self._register_marketplace_routes(app, auth, _ctx, _skills, _llm)

        # ---------------------------------------------------------------- Self-improvement endpoints
        self._register_improvement_routes(app, auth, _ctx)

        # ---------------------------------------------------------------- Cost tracking endpoints
        self._register_cost_routes(app, auth, _llm)

        # ---------------------------------------------------------------- Session endpoints
        self._register_sessions_routes(app, auth, _ctx)

        # ---------------------------------------------------------------- Settings
        self._register_settings_routes(app, auth, _ctx, _get_backup_mgr)

        # ---------------------------------------------------------------- Config Backup
        self._register_config_backup_routes(app, auth, _get_backup_mgr)

        # ---------------------------------------------------------------- Document RAG
        self._register_document_routes(app, auth, _ctx)

        # ---------------------------------------------------------------- Alerting
        self._register_alerting_routes(app, auth, _ctx)

        # ---------------------------------------------------------------- Approval (Human-in-the-Loop)
        self._register_approval_routes(app, auth, _ctx)

        # ── MCP (Model Context Protocol) 端点 ───────────────────────────────────────────────────────────
        self._register_mcp_routes(app, auth, _ctx)

        @app.exception_handler(Exception)
        async def all_exception(request: Request, exc: Exception):
            from starlette.exceptions import HTTPException as StarletteHTTPException

            from core.exceptions import InputValidationError, OneAgentError, SecurityError

            if isinstance(exc, StarletteHTTPException):
                status_code = getattr(exc, "status_code", 500)
                return JSONResponse(
                    {"error": {"code": status_code, "message": exc.detail}},
                    status_code=status_code,
                )
            # Handle custom One-Agent exceptions
            if isinstance(exc, InputValidationError):
                return JSONResponse(
                    {"error": {"code": 400, "message": str(exc), "type": "InputValidationError"}},
                    status_code=400,
                )
            if isinstance(exc, SecurityError):
                return JSONResponse(
                    {"error": {"code": 403, "message": str(exc), "type": "SecurityError"}},
                    status_code=403,
                )
            if isinstance(exc, OneAgentError):
                logger.exception("one-agent error: %s", exc)
                return JSONResponse(
                    {"error": {"code": 500, "message": str(exc), "type": "OneAgentError"}},
                    status_code=500,
                )
            # Generic exception handler
            logger.exception("api error: %s", exc)
            return JSONResponse(
                {"error": {"code": 500, "message": _("internal_error"), "type": "internal_error"}},
                status_code=500,
            )

        self._app = app
        try:
            import uvicorn
            # 修复：uvicorn 0.30+ 移除了 Config 的 capture_output 参数。
            # 用 try/except 兼容新旧版本。
            try:
                config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning", capture_output=False)
            except TypeError:
                config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning")
            server = uvicorn.Server(config)
            self._task = __import__("asyncio").create_task(server.serve())
            logger.info("REST API running on http://%s:%d", self._host, self._port)
        except Exception as exc:
            logger.warning("could not start REST API: %s", exc)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                # We don't care about shutdown errors — just make sure
                # the task is awaited so we don't leak the unhandled
                # "Task was destroyed but it is pending" warning.
                pass
        await super().stop()
