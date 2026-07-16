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


def _resolve_config_path() -> str:
    """解析当前生效的配置文件路径。

    与 one_agent.py 的 _get_config_path() 保持一致，避免"读 dev、写 default"
    的路径错位 bug。优先级：
      1. ONE_AGENT_CONFIG 环境变量（显式指定，最高优先级）
      2. ONE_AGENT_ENV 环境变量 → config/{env}_config.yaml
      3. config/default_config.yaml（兜底默认）
    """
    import os
    explicit = os.environ.get("ONE_AGENT_CONFIG")
    if explicit:
        return explicit
    env_name = os.environ.get("ONE_AGENT_ENV", "").strip().lower()
    if env_name:
        env_path = Path(__file__).resolve().parent.parent / "config" / f"{env_name}_config.yaml"
        if env_path.exists():
            return str(env_path)
    return str(Path(__file__).resolve().parent.parent / "config" / "default_config.yaml")


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

        # ===== 接入 HealthChecker：注册基于实际实例的检查函数 =====
        # monitor/health.py 中 HealthChecker 完整实现了检查框架，
        # 但 get_health_checker() 默认注册的内置检查使用硬编码路径。
        # 这里用实际的 _ctx/_llm/_memory_plugin 实例覆盖默认检查，
        # 让 /health 和 /ready 端点调用真实的子系统状态而非静态 dict。
        from monitor.health import (
            ComponentCheck,
            HealthStatus,
            _check_disk_space,
            get_health_checker,
        )

        _health_checker = get_health_checker()

        def _check_db_with_ctx() -> ComponentCheck:
            try:
                _session_store = _cp("session_store")
                if _session_store:
                    count = _session_store.get_session_count()
                    return ComponentCheck(
                        name="database",
                        status=HealthStatus.HEALTHY,
                        message=f"Accessible, {count} sessions",
                        details={"session_count": count},
                    )
                return ComponentCheck(
                    name="database",
                    status=HealthStatus.DEGRADED,
                    message="session_store not available",
                )
            except sqlite3.Error as e:
                return ComponentCheck(
                    name="database",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Database error: {e}",
                )
            except Exception as e:
                return ComponentCheck(
                    name="database",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Database check failed: {e}",
                )

        def _check_memory_with_ctx() -> ComponentCheck:
            try:
                if _memory_plugin is None:
                    return ComponentCheck(
                        name="memory",
                        status=HealthStatus.DEGRADED,
                        message="Memory plugin not available",
                    )
                stats = _memory_plugin.stats() if hasattr(_memory_plugin, "stats") else {}
                rows = stats.get("rows", 0) if isinstance(stats, dict) else 0
                return ComponentCheck(
                    name="memory",
                    status=HealthStatus.HEALTHY,
                    message=f"Memory OK, {rows} rows",
                    details={"rows": rows},
                )
            except Exception as e:
                return ComponentCheck(
                    name="memory",
                    status=HealthStatus.DEGRADED,
                    message=f"Memory check failed: {e}",
                )

        def _check_llm_with_ctx() -> ComponentCheck:
            try:
                if _llm is None:
                    return ComponentCheck(
                        name="llm_provider",
                        status=HealthStatus.DEGRADED,
                        message="LLM provider not available",
                    )
                stats = _llm.stats() if hasattr(_llm, "stats") else {}
                calls = stats.get("calls", 0) if isinstance(stats, dict) else 0
                provider = getattr(_llm, "_primary_provider", "unknown")
                return ComponentCheck(
                    name="llm_provider",
                    status=HealthStatus.HEALTHY,
                    message=f"LLM OK, provider={provider}, calls={calls}",
                    details={"provider": provider, "calls": calls},
                )
            except Exception as e:
                return ComponentCheck(
                    name="llm_provider",
                    status=HealthStatus.UNHEALTHY,
                    message=f"LLM check failed: {e}",
                )

        def _check_config_with_ctx() -> ComponentCheck:
            try:
                if _ctx is None or not getattr(_ctx, "config", None):
                    return ComponentCheck(
                        name="config",
                        status=HealthStatus.UNHEALTHY,
                        message="Context or config not available",
                    )
                return ComponentCheck(
                    name="config",
                    status=HealthStatus.HEALTHY,
                    message="Config loaded",
                )
            except Exception as e:
                return ComponentCheck(
                    name="config",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Config error: {e}",
                )

        # 覆盖默认检查（同名注册会替换 get_health_checker() 中的内置版本）
        _health_checker.register_check("database", _check_db_with_ctx)
        _health_checker.register_check("memory", _check_memory_with_ctx)
        _health_checker.register_check("llm_provider", _check_llm_with_ctx)
        _health_checker.register_check("config", _check_config_with_ctx)
        _health_checker.register_check("disk_space", _check_disk_space)

        @app.get("/health")
        async def health_check():
            """Basic health check - service is alive.

            调用 HealthChecker.check_all() 获取真实的子系统状态；
            若 HealthChecker 抛异常则 fallback 到原有的静态返回。
            """
            try:
                return _health_checker.check_all()
            except Exception as exc:
                logger.warning("HealthChecker.check_all() failed, fallback to static: %s", exc)
                return {
                    "status": "healthy",
                    "timestamp": time.time(),
                    "version": "2.0.0"
                }

        @app.get("/ready")
        async def readiness_check():
            """Readiness check - service can handle requests.

            使用 HealthChecker.check_readiness() 的检查结果；
            若 HealthChecker 抛异常则 fallback 到原有的静态检查逻辑。
            """
            try:
                return _health_checker.check_readiness()
            except Exception as exc:
                logger.warning("HealthChecker.check_readiness() failed, fallback to static: %s", exc)

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
                from one_agent import load_config

                config_path = _resolve_config_path()
                new_config = load_config(config_path)

                # Update context config — must be a dict to match startup type
                # (one_agent.py sets _ctx.config = config.model_dump()). Pydantic
                # BaseModel has no .get(), so assigning the model object directly
                # would crash every downstream ctx.config.get(...) call.
                _ctx.config = new_config.model_dump() if hasattr(new_config, "model_dump") else new_config

                # Update API-specific settings
                if hasattr(self, '_setup_from_config'):
                    self._setup_from_config(new_config)

                # 热重载 LLM Provider：之前只更新了 _ctx.config 和 REST 设置，
                # 但 LLMProvider 的 _api_keys/_primary_provider/_default_model/
                # _provider_base_urls 仍是启动时缓存的值，导致客户端改了配置
                # 也不生效（必须重启服务）。现在重新 setup 让配置即时生效。
                # 问题11 修复：_register_dashboard_config_routes 没有 _llm 参数，
                # 必须从 _ctx.get_plugin("llm") 获取，否则 NameError 导致
                # /api/config/reload 请求 500 失败。
                _llm_inst = _ctx.get_plugin("llm") if _ctx else None
                if _llm_inst is not None:
                    try:
                        import asyncio
                        async def _resync_llm():
                            await _llm_inst.setup(_ctx)
                        try:
                            # 问题1 修复：同步等待 setup 完成，不能用 create_task
                            # fire-and-forget（否则返回后 _api_keys 未更新）
                            await _resync_llm()
                        except RuntimeError:
                            asyncio.run(_resync_llm())
                        logger.info("LLM provider re-synced after config reload")
                    except Exception as exc:
                        logger.warning("LLM re-sync failed after reload: %s", exc)

                logger.info("Configuration reloaded successfully")

                return {
                    "status": "ok",
                    "message": "Configuration reloaded",
                    "timestamp": time.time()
                }
            except Exception as e:
                logger.error("Failed to reload config: %s", e, exc_info=True)
                raise HTTPException(500, f"Config reload failed: {str(e)}")

        @app.put("/api/config")
        async def update_config(
            body: Dict[str, Any],
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """Update configuration values and save to file.

            Accepts a partial config dict (only the fields to update),
            deep-merges them into the current config, writes to the
            config file, and triggers a hot reload.

            Sensitive fields (api_keys, passwords) are validated but
            not returned in the response.
            """
            auth(x_api_key)

            if _ctx is None:
                raise HTTPException(503, "Context not initialized")

            try:
                import copy

                from one_agent import load_config

                # 修复：使用 _resolve_config_path() 而非只读 ONE_AGENT_CONFIG，
                # 与启动时的 _get_config_path() 保持一致，避免"读 dev、写 default"
                # 的路径错位 bug。
                config_path = _resolve_config_path()

                # Load raw config from file (not runtime config, which may
                # have masked API keys) so we can safely merge and save.
                raw_config = load_config(config_path)
                config_dict = (
                    raw_config.model_dump()
                    if hasattr(raw_config, "model_dump")
                    else copy.deepcopy(raw_config)
                )

                # Deep merge the incoming updates.
                # 修复：处理 "***" 哨兵值 — GET /api/config 会把 api_keys 脱敏成
                # "***"，如果客户端原样回传，_deep_merge 会用字面量 "***" 覆盖
                # 真实密钥。现在遇到 "***" 时跳过，保留文件中的原值。
                def _deep_merge(target: dict, source: dict) -> dict:
                    for key, value in source.items():
                        # 跳过 "***" 哨兵（GET 脱敏值，客户端未修改该字段）
                        if value == "***":
                            continue
                        if (
                            key in target
                            and isinstance(target[key], dict)
                            and isinstance(value, dict)
                        ):
                            _deep_merge(target[key], value)
                        else:
                            target[key] = value
                    return target

                updates = body.get("config") or body  # accept both {config:{...}} and direct dict
                _deep_merge(config_dict, updates)

                # Validate through Pydantic if available
                if hasattr(raw_config, "model_validate"):
                    try:
                        raw_config.model_validate(config_dict)
                    except Exception as e:
                        raise HTTPException(400, f"Config validation failed: {e}")

                # Save to file
                import yaml

                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(
                        config_dict,
                        f,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )

                # Hot reload runtime config
                new_config = load_config(config_path)
                _ctx.config = (
                    new_config.model_dump()
                    if hasattr(new_config, "model_dump")
                    else new_config
                )

                # Update API-specific settings
                if hasattr(self, "_setup_from_config"):
                    self._setup_from_config(new_config)

                # 修复：热重载 LLM Provider。之前 PUT 后只更新了 _ctx.config
                # 和 REST 设置，但 LLMProvider 的 _api_keys/_primary_provider/
                # _default_model/_provider_base_urls 仍是启动时缓存的值，
                # 导致客户端改了 base_urls/primary_model/primary_provider 也不
                # 生效（必须重启服务）。现在重新 setup 让配置即时生效。
                # 问题11 修复：_register_dashboard_config_routes 没有 _llm 参数，
                # 必须从 _ctx.get_plugin("llm") 获取，否则 NameError 导致
                # 所有 PUT /api/config 请求 500 失败（"保存失败"根因之一）。
                # 问题1 修复：LLM 热重载必须同步等待完成，不能用 create_task
                # fire-and-forget。之前用 loop.create_task() 不 await，导致
                # update_config 返回后 _llm._api_keys 仍未更新，客户端紧接着
                # 调用 list_providers 时 has_key 返回 False（即使 key 已保存
                # 到文件），新增服务商在"添加服务商"对话框中显示为"未配置"。
                _llm_inst = _ctx.get_plugin("llm") if _ctx else None
                if _llm_inst is not None:
                    try:
                        import asyncio
                        async def _resync_llm():
                            await _llm_inst.setup(_ctx)
                        try:
                            loop = asyncio.get_running_loop()
                            # 同步等待 setup 完成，确保 _api_keys 立即更新
                            await _resync_llm()
                        except RuntimeError:
                            asyncio.run(_resync_llm())
                        logger.info("LLM provider re-synced after config update")
                    except Exception as exc:
                        logger.warning("LLM re-sync failed after config update: %s", exc)

                # 问题1+3 修复：热重载 SmartRouter，让 router.enabled / self_evolution /
                # context_compression 等配置即时生效。之前只热重载 LLM，router._cfg
                # 仍是启动时的缓存值，导致 /api/models 返回的 routing_enabled 过期，
                # 客户端开关状态不同步。
                _router_inst = _ctx.get_plugin("router") if _ctx else None
                if _router_inst is not None:
                    try:
                        _router_inst._cfg = (_ctx.config.get("router") or {})
                        logger.info("Router config re-synced after config update")
                    except Exception as exc:
                        logger.warning("Router re-sync failed after config update: %s", exc)

                logger.info("Configuration updated and saved to %s", config_path)

                # Return sanitized config
                sanitized = copy.deepcopy(config_dict)
                if "llm" in sanitized and "api_keys" in sanitized["llm"]:
                    sanitized["llm"]["api_keys"] = {
                        k: "***" if v else None
                        for k, v in sanitized["llm"]["api_keys"].items()
                    }

                return {
                    "status": "ok",
                    "message": "Configuration updated",
                    "config": sanitized,
                    "timestamp": time.time(),
                }
            except HTTPException:
                raise
            except Exception as e:
                logger.error("Failed to update config: %s", e, exc_info=True)
                raise HTTPException(500, f"Config update failed: {str(e)}")

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

            # Router statistics — tier distribution and routing counters
            router_data = {}
            _router = _ctx.get_plugin("router") if _ctx else None
            if _router:
                try:
                    router_data = {
                        "enabled": getattr(_router, "_cfg", {}).get("enabled", True),
                        "tier_stats": getattr(_router, "_tier_stats", {}),
                        "total_routed": sum(
                            s.get("picked", 0)
                            for s in getattr(_router, "_tier_stats", {}).values()
                        ),
                    }
                except Exception as exc:
                    logger.warning("router stats query failed: %s", exc)

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
                "counters": _ctx.counters if _ctx else {},
                "router": router_data,
            }

        @app.get("/api/models")
        async def models(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Model catalog with classification, tier mapping, and routing status.

            Returns the default model, 4-tier smart routing configuration,
            available models grouped by capability (text/image/video/etc),
            and per-tier statistics.
            """
            auth(x_api_key)

            from models.tiers import MODEL_TIERS

            default_model = getattr(_llm, "_default_model", "") if _llm else ""
            primary_provider = getattr(_llm, "_primary_provider", "") if _llm else ""

            # Routing status — 从 _ctx.config 读取（配置更新的真实源头），
            # 而非 _router._cfg（运行时缓存，PUT 后未热重载 router 时会过期）。
            # 问题1+3 根因：之前从 _router._cfg 读，导致客户端关闭路由后
            # /api/models 仍返回 routing_enabled=true，开关状态不同步。
            _router = _ctx.get_plugin("router") if _ctx else None
            router_cfg = (_ctx.config.get("router") or {}) if _ctx else {}
            routing_enabled = router_cfg.get("enabled", True)
            tier_stats = getattr(_router, "_tier_stats", {}) if _router else {}

            # 4-tier model mapping with thresholds from config
            router_cfg = (_ctx.config.get("router") or {}) if _ctx else {}
            thresholds = router_cfg.get("task_complexity_thresholds", {})
            token_budgets = {"trivial": 512, "simple": 1024, "complex": 2048, "expert": 4096}
            tiers = {}
            for tier_name in ["trivial", "simple", "complex", "expert"]:
                tiers[tier_name] = {
                    "models": MODEL_TIERS.get(tier_name, []),
                    "threshold": thresholds.get(tier_name, 0.0),
                    "token_budget": token_budgets.get(tier_name, 1024),
                    "stats": tier_stats.get(tier_name, {"picked": 0, "rerouted_up": 0, "rerouted_down": 0}),
                }

            # Model catalog — best-effort, may be empty if no API key or network
            available_models = []
            models_by_category: Dict[str, List[str]] = {}
            if _llm:
                try:
                    cat = _llm.get_catalog()
                    if cat:
                        await asyncio.wait_for(cat.refresh(), timeout=10.0)
                        # 问题11 修复：ModelCatalog 的方法是 all()，不是 all_models()。
                        # 之前调用 cat.all_models() 会抛 AttributeError，被外层
                        # except 吞掉，导致 /api/models 永远返回空的 available_models。
                        for m in cat.all():
                            md = m.to_dict()
                            md["supports_tools"] = _llm.model_supports_tools(m.id)
                            available_models.append(md)
                            for cap in m.capabilities:
                                models_by_category.setdefault(cap, []).append(m.id)
                except asyncio.TimeoutError:
                    logger.debug("model catalog refresh timed out")
                except Exception as exc:
                    logger.debug("model catalog unavailable: %s", exc)

            return {
                "default_model": default_model,
                "primary_provider": primary_provider,
                "routing_enabled": routing_enabled,
                "tiers": tiers,
                "available_models": available_models,
                "models_by_category": models_by_category,
            }

        @app.get("/api/logs")
        async def get_logs(
            tail: int = 200,
            level: Optional[str] = None,
            search: Optional[str] = None,
            since: Optional[float] = None,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """查看 one-agent 日志。
            - tail: 返回最后 N 行（默认 200，最大 2000）
            - level: 按级别过滤 (DEBUG/INFO/WARNING/ERROR)
            - search: 关键词搜索（不区分大小写）
            - since: Unix 时间戳，只返回该时间之后的日志（用于"本次启动"过滤）
            """
            auth(x_api_key)
            from pathlib import Path
            import re as _re
            data_dir = (_ctx.config.get("agent", {}).get("data_dir", "./data")) if _ctx else "./data"
            log_path = Path(data_dir) / "logs" / "one_agent.log"
            if not log_path.exists():
                return {"lines": [], "total": 0, "filtered": 0}
            tail = min(max(tail, 1), 2000)
            # 默认 since = 本次启动时间（如果未指定）
            if since is None and _ctx:
                since = getattr(_ctx, "started_at", None)
            try:
                # 读取全部日志文件（含轮转备份按时间顺序）
                log_files = sorted(log_path.parent.glob("one_agent.log*"))
                all_lines: List[str] = []
                for lf in log_files:
                    try:
                        all_lines.extend(lf.read_text(encoding="utf-8", errors="replace").splitlines())
                    except Exception:
                        continue
                # 限制总行数避免内存爆炸
                all_lines = all_lines[-50000:] if len(all_lines) > 50000 else all_lines
                total = len(all_lines)
                # 按时间过滤（since 之后的日志）
                if since is not None:
                    # 日志格式：2026-07-15 10:23:45 | LEVEL | logger | msg
                    ts_pattern = _re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
                    from datetime import datetime as _dt
                    cutoff_dt = _dt.fromtimestamp(since)
                    filtered_by_time: List[str] = []
                    for line in all_lines:
                        m = ts_pattern.match(line)
                        if m:
                            try:
                                line_dt = _dt.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                                if line_dt >= cutoff_dt:
                                    filtered_by_time.append(line)
                            except ValueError:
                                filtered_by_time.append(line)
                        else:
                            # 无时间戳的行（多行日志的续行）保留
                            filtered_by_time.append(line)
                    all_lines = filtered_by_time
                # 按级别过滤
                if level:
                    level_upper = level.upper()
                    all_lines = [l for l in all_lines if f"| {level_upper:<7}" in l or f"| {level_upper} |" in l]
                # 关键词搜索
                if search:
                    search_lower = search.lower()
                    all_lines = [l for l in all_lines if search_lower in l.lower()]
                filtered = len(all_lines)
                # 取最后 tail 行
                result_lines = all_lines[-tail:] if len(all_lines) > tail else all_lines
            except Exception as exc:
                logger.debug("read logs failed: %s", exc)
                return {"lines": [], "total": 0, "filtered": 0, "error": str(exc)}
            return {"lines": result_lines, "total": total, "filtered": filtered}

        @app.post("/api/models/test")
        async def test_model(
            body: dict,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """测试模型是否可用。body: {"model": "provider/model", "api_key": "..."}"""
            auth(x_api_key)
            if _llm is None:
                raise HTTPException(503, "LLM provider not available")
            model = (body.get("model") or "").strip()
            if not model:
                raise HTTPException(400, "model is required")
            test_key = body.get("api_key", "")
            try:
                # 发送一个简短的测试请求
                import asyncio as _aio
                test_messages = [{"role": "user", "content": "Hi"}]
                # 如果提供了 api_key，临时设置
                if test_key and "/" in model:
                    provider = model.split("/")[0]
                    _llm.set_api_key(provider, test_key)
                # 尝试流式调用（最轻量）
                chunks = []
                async for chunk in _llm.chat_completion_stream(
                    messages=test_messages,
                    model=model,
                    max_tokens=10,
                    temperature=0.1,
                ):
                    delta = chunk.get("delta", "")
                    if delta:
                        chunks.append(delta)
                    if len(chunks) >= 3:
                        break
                response_text = "".join(chunks)
                return {
                    "ok": True,
                    "model": model,
                    "response": response_text[:100],
                    "message": "模型可用",
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "model": model,
                    "error": str(exc)[:200],
                    "message": f"模型测试失败: {exc}",
                }

        @app.post("/api/models/providers")
        async def list_providers(
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """列出所有已知服务商及其 base_url。

            问题1 修复：除了 KNOWN_PROVIDERS 硬编码列表外，还扫描
            _llm._api_keys 和 config.llm.base_urls 中所有已配置的服务商
            （包括用户自定义添加的服务商），确保它们也出现在列表中。
            之前只返回 KNOWN_PROVIDERS，自定义服务商永远不在列表里，
            导致切换主服务商后客户端 configuredProviders 如果有任何
            边缘情况丢失，自定义服务商就从 UI 消失。
            """
            auth(x_api_key)
            from models.resolver import KNOWN_PROVIDERS
            # 获取已配置的 api_keys
            # 优先从 _llm._api_keys 读取（运行时缓存），回退到 _ctx.config
            configured_keys = {}
            if _llm:
                configured_keys = getattr(_llm, "_api_keys", {}) or {}
            # 问题1 修复：_llm 可能为 None 或 _api_keys 未及时更新，
            # 从 _ctx.config 读取作为补充来源
            cfg_api_keys = (_ctx.config.get("llm", {}) or {}).get("api_keys", {}) or {}
            for k, v in cfg_api_keys.items():
                if k not in configured_keys or not configured_keys.get(k):
                    if v:  # 只在有值时补充
                        configured_keys[k] = v

            # 获取 base_urls 映射（含自定义服务商的 base_url）
            base_urls = {}
            if _llm:
                base_urls.update(getattr(_llm, "_provider_base_urls", {}) or {})
            # 合并 config.llm.base_urls（自定义服务商的 base_url）
            cfg_base_urls = (_ctx.config.get("llm", {}) or {}).get("base_urls", {}) or {}
            base_urls.update(cfg_base_urls)

            providers = []
            seen_names = set()
            # 1. 先加入 KNOWN_PROVIDERS
            for name, base_url in KNOWN_PROVIDERS.items():
                seen_names.add(name)
                providers.append({
                    "name": name,
                    "base_url": base_url,
                    "has_key": bool(configured_keys.get(name)),
                })
            # 2. 再加入 _api_keys 中有 key 但不在 KNOWN_PROVIDERS 的自定义服务商
            for name, key_val in configured_keys.items():
                if name in seen_names:
                    continue
                if not key_val:
                    continue
                seen_names.add(name)
                providers.append({
                    "name": name,
                    "base_url": base_urls.get(name, ""),
                    "has_key": True,
                })
            # 3. 最后加入 base_urls 中有 URL 但不在以上列表的服务商
            for name, url in base_urls.items():
                if name in seen_names:
                    continue
                if not url:
                    continue
                seen_names.add(name)
                providers.append({
                    "name": name,
                    "base_url": url,
                    "has_key": bool(configured_keys.get(name)),
                })
            return {"providers": providers}

        @app.post("/api/models/providers/test")
        async def test_provider(
            body: dict,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """测试服务商连通性并拉取模型列表（含分类与元数据）。

            返回结构::
                {
                  "ok": True,
                  "provider": "openai",
                  "base_url": "...",
                  "models_count": 50,
                  "models": ["gpt-4o", ...],            # 向后兼容：纯 id 列表
                  "free_models": [ {ModelInfo.to_dict()}, ... ],
                  "paid_models": [ {ModelInfo.to_dict()}, ... ],
                }

            分类逻辑：使用 ``ModelCatalog._normalize`` + ``auto_classify_tier``
            对每个模型做元数据补全（is_free / pricing / description / tier /
            capabilities），按 is_free 拆分为 free / paid 两组，方便客户端
            分开展示。原始 ``models`` 字段保留为纯 id 列表以向后兼容。
            """
            auth(x_api_key)
            provider = (body.get("provider") or "").strip()
            api_key = body.get("api_key", "")
            base_url = (body.get("base_url") or "").strip()
            if not provider:
                raise HTTPException(400, "provider is required")
            # 修复：客户端编辑已配置服务商时 key 字段可能留空（表示不修改），
            # 此时使用服务端已存储的 key 进行测试，而非发送无 auth 的请求。
            if not api_key and _llm is not None:
                try:
                    stored = getattr(_llm, "_api_keys", {}).get(provider, "")
                    if stored and stored != "***":
                        api_key = stored
                except Exception:
                    pass
            if not base_url:
                from models.resolver import KNOWN_PROVIDERS
                base_url = KNOWN_PROVIDERS.get(provider, "")
                # 也尝试从 LLMProvider 的自定义 base_urls 读取
                if not base_url and _llm is not None:
                    try:
                        base_url = getattr(_llm, "_provider_base_urls", {}).get(provider, "")
                    except Exception:
                        pass
            if not base_url:
                raise HTTPException(400, f"unknown provider: {provider}, please provide base_url")
            try:
                # 使用 ModelCatalog 拉取并分类模型（含 is_free / pricing /
                # description / tier / capabilities 等元数据），让客户端可以
                # 分免费/付费两组展示，并显示模型介绍。
                from models.catalog import ModelCatalog, auto_classify_tier
                from models.capabilities import detect_capabilities
                cat = ModelCatalog(base_url=base_url, api_key=api_key, provider=provider, ttl=0)
                try:
                    await cat.refresh(force=True)
                finally:
                    await cat.aclose()

                free_models = []
                paid_models = []
                plain_ids = []
                for m in cat.all():
                    # 补全能力信息（text/vision/tools/reasoning 等）
                    try:
                        m.capabilities = detect_capabilities(m)
                    except Exception:
                        pass
                    md = m.to_dict()
                    md["supports_tools"] = bool(_llm and _llm.model_supports_tools(m.id)) if _llm else False
                    plain_ids.append(m.id)
                    if m.is_free:
                        free_models.append(md)
                    else:
                        paid_models.append(md)

                return {
                    "ok": True,
                    "provider": provider,
                    "base_url": base_url,
                    "models_count": len(plain_ids),
                    "models": plain_ids[:50],  # 向后兼容
                    "free_models": free_models,
                    "paid_models": paid_models,
                }
            except Exception as exc:
                logger.warning("test_provider(%s) failed: %s", provider, exc)
                return {"ok": False, "provider": provider, "error": str(exc)[:200]}

        @app.get("/api/marketplace/browse")
        async def browse_marketplace(
            query: str = "",
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """从公开社区市场浏览可用技能（GitHub one-agent-skills 仓库）。"""
            auth(x_api_key)
            mp_plugin = _ctx.marketplace_plugin if _ctx and hasattr(_ctx, "marketplace_plugin") else None
            if mp_plugin is None and _ctx:
                mp_plugin = getattr(_ctx, "marketplace_plugin", None)
            if mp_plugin is None:
                # 尝试直接创建临时实例拉取
                try:
                    from marketplace import MarketplacePlugin
                    mp_plugin = MarketplacePlugin()
                except Exception:
                    return {"packages": [], "error": "marketplace plugin not available"}
            try:
                packages = await mp_plugin.browse_registry(query)
                return {"packages": packages}
            except Exception as exc:
                return {"packages": [], "error": str(exc)[:200]}

        @app.post("/api/marketplace/install_url")
        async def install_from_url(
            body: dict,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """从 GitHub URL 或 owner/repo/path 安装技能到社区目录。"""
            auth(x_api_key)
            source = (body.get("source") or "").strip()
            if not source:
                raise HTTPException(400, "source is required")
            mp_plugin = _ctx.marketplace_plugin if _ctx and hasattr(_ctx, "marketplace_plugin") else None
            if mp_plugin is None and _ctx:
                mp_plugin = getattr(_ctx, "marketplace_plugin", None)
            if mp_plugin is None:
                try:
                    from marketplace import MarketplacePlugin
                    mp_plugin = MarketplacePlugin()
                except Exception:
                    raise HTTPException(503, "marketplace plugin not available")
            try:
                result = await mp_plugin.install(source)
                return result
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:200]}

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

            # ===== 接入 MetricsRegistry =====
            # monitor/prometheus.py 的 MetricsRegistry 完整实现了
            # Counter/Gauge/Histogram 及 Prometheus 文本格式化。
            # 若注册表中已有实际指标值（被其它代码 inc/set/observe 过），
            # 用注册表的格式化输出；否则 fallback 到下面的手拼文本。
            try:
                from monitor.prometheus import get_metrics_registry
                _metrics_reg = get_metrics_registry()
                _collected = _metrics_reg.collect_all()
                _has_real_metrics = any(
                    len(values) > 0 for _, _, _, values in _collected
                )
                if _has_real_metrics:
                    from fastapi.responses import PlainTextResponse
                    return PlainTextResponse(
                        _metrics_reg.format_prometheus(),
                        media_type="text/plain; version=0.0.4",
                    )
            except Exception as exc:
                logger.warning("MetricsRegistry failed, fallback to hand-written metrics: %s", exc)

            # ===== Fallback：原有手拼文本逻辑（保留不变）=====
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

            # 优先使用客户端传递的语言，没有则自动检测
            from i18n import detect_language, set_thread_language
            client_lang = body.get("language")
            if client_lang and isinstance(client_lang, str) and client_lang.strip():
                detected_lang = client_lang.strip().lower()
            elif text:
                detected_lang = detect_language(text)
            else:
                detected_lang = "zh"
            set_thread_language(detected_lang)
            safe_lang = str(detected_lang).replace('\n', '\\n').replace('\r', '\\r')
            logger.info("Language set to: %s (chat)", safe_lang)

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

            # 优先使用客户端传递的语言，没有则自动检测
            from i18n import detect_language, set_thread_language
            client_lang = body.get("language")
            if client_lang and isinstance(client_lang, str) and client_lang.strip():
                detected_lang = client_lang.strip().lower()
            elif text:
                detected_lang = detect_language(text)
            else:
                detected_lang = "zh"
            set_thread_language(detected_lang)
            logger.info("Language set to: %s (stream)", detected_lang)

            from fastapi.responses import StreamingResponse

            # 接入 core/streaming.py 的 SSEFormatter：收口 SSE 数据帧的
            # JSON 序列化 + ``data: ...\n\n`` 包装，替代手动拼
            # ``f"data: {json.dumps(data)}\n\n"``。FastAPI 的 StreamingResponse
            # 仍负责传输层，这里只在数据格式化层走 SSEFormatter。
            from core.streaming import SSEFormatter

            async def event_generator():
                yield SSEFormatter.format_data({'status': 'thinking', 'session_id': session_id})

                try:
                    if _app_instance and hasattr(_app_instance, "chat_with_thinking"):
                        # 订阅 turn_progress 事件，实时推送思考进度到客户端
                        # 让用户在等待回复时能看到 agent 正在做什么
                        import asyncio as _asyncio
                        # maxsize 防止消费端卡住时无限增长 OOM
                        progress_queue: _asyncio.Queue = _asyncio.Queue(maxsize=100)

                        # 推送这些 phase 到客户端：
                        # thinking 类 phase → 客户端思考卡片
                        # streaming phase → 客户端主聊天区（最终答案实时增量）
                        # tool_result phase → 客户端思考卡片（工具执行结果）
                        _ALLOWED_PHASES = {
                            "planning", "thinking", "reflection", "plan",
                            "multi_agent", "deep_research", "comparison",
                            "reasoning", "tool_loop", "regeneration",
                            "skill_dispatch", "chart", "tool_result",
                            "streaming",
                            # 补充：coordinator 实际使用的但之前遗漏的 phase
                            "agent_mesh", "batch", "eval", "model_compare",
                            "provider_resolve", "rewrite", "verification",
                        }

                        def _on_progress(evt) -> None:
                            # 只推送当前 session 的进度
                            if evt.get("session_id") != session_id:
                                return
                            try:
                                msg = evt.get("message", "")
                                phase = evt.get("phase", "")
                                # 过滤掉不相关的 phase（流式回复、工具结果等）
                                if phase and phase not in _ALLOWED_PHASES:
                                    return
                                if not msg:
                                    return
                                # 直接放入队列（_on_progress 在同一个 event loop 中被调用，无需 call_soon_threadsafe）
                                # 满了直接丢弃（进度事件不要求可靠）
                                try:
                                    progress_queue.put_nowait((msg, phase))
                                except _asyncio.QueueFull:
                                    pass
                            except Exception as _e:
                                # 进度事件处理失败不应中断主流程，但记录便于排查
                                logger.debug("progress event handling failed: %s", _e, exc_info=True)

                        _app_instance.bus.subscribe("turn_progress", _on_progress)

                        # 并行运行 chat_with_thinking 和进度消费
                        chat_task = _asyncio.create_task(
                            _app_instance.chat_with_thinking(
                                text, source="api", session_id=session_id
                            )
                        )

                        result: Dict[str, Any] = {}
                        _streamed_content = False  # 跟踪是否已通过 streaming phase 推送了最终答案
                        _last_heartbeat = _asyncio.get_running_loop().time()
                        try:
                            while not chat_task.done():
                                # 修复：检查客户端是否断连（避免 LLM token 浪费）
                                if await request.is_disconnected():
                                    logger.info("Client disconnected during stream, cancelling chat task")
                                    chat_task.cancel()
                                    break
                                try:
                                    # 用 wait_for 避免任务泄漏（超时自动取消 coroutine）
                                    msg, phase = await _asyncio.wait_for(
                                        progress_queue.get(), timeout=0.5
                                    )
                                    _last_heartbeat = _asyncio.get_running_loop().time()
                                    if phase == "streaming":
                                        # streaming phase 是最终答案的实时增量，作为 content 推送
                                        _streamed_content = True
                                        yield SSEFormatter.format_data({'content': msg, 'session_id': session_id})
                                    else:
                                        yield SSEFormatter.format_data({'status': 'thinking', 'content': msg, 'phase': phase, 'session_id': session_id})
                                except _asyncio.TimeoutError:
                                    # 心跳保活：长时间无进度事件时发送 heartbeat，防止客户端超时断连
                                    now = _asyncio.get_running_loop().time()
                                    if now - _last_heartbeat >= 10:
                                        _last_heartbeat = now
                                        yield SSEFormatter.format_data({'status': 'heartbeat', 'session_id': session_id})
                            # chat_task 完成后，消费队列中剩余的事件
                            while not progress_queue.empty():
                                try:
                                    msg, phase = progress_queue.get_nowait()
                                    if not msg:
                                        continue
                                    if phase == "streaming":
                                        _streamed_content = True
                                        yield SSEFormatter.format_data({'content': msg, 'session_id': session_id})
                                    else:
                                        yield SSEFormatter.format_data({'status': 'thinking', 'content': msg, 'phase': phase, 'session_id': session_id})
                                except _asyncio.QueueEmpty:
                                    break
                        finally:
                            _app_instance.bus.unsubscribe("turn_progress", _on_progress)
                            if not chat_task.done():
                                chat_task.cancel()
                            try:
                                # 超时保护：取消后任务应快速抛出 CancelledError，
                                # 但若底层协程吞掉取消或卡在阻塞调用，await 会无限挂起，
                                # 导致 SSE 流永不关闭、客户端超时。限时 5s 等待结果。
                                result = await _asyncio.wait_for(chat_task, timeout=5.0)
                            except _asyncio.TimeoutError:
                                logger.warning("chat_task did not finish within 5s after cancel; abandoning")
                            except _asyncio.CancelledError:
                                pass
                            except Exception as exc:
                                logger.error("chat_task failed: %s", exc, exc_info=True)

                        reply = result.get("reply", "")
                        thinking = result.get("thinking", "")
                        # 最终的完整思考计划用 phase=plan 标记（客户端会覆盖之前的截断版）
                        # 即使 thinking 为空也必须推送，否则客户端会一直保留初始占位"思考中..."
                        yield SSEFormatter.format_data({'status': 'thinking', 'content': thinking, 'phase': 'plan', 'session_id': session_id})
                        # 如果已通过 streaming phase 实时推送了最终答案，不再重复推送
                        if reply and not _streamed_content:
                            yield SSEFormatter.format_data({'content': reply, 'session_id': session_id})
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
                            yield SSEFormatter.format_data(chunk)
                            chunks_sent += 1
                    else:
                        yield SSEFormatter.format_data({'error': 'LLM provider not available', 'session_id': session_id})
                except Exception as exc:
                    logger.error("stream chat error: %s", exc, exc_info=True)
                    yield SSEFormatter.format_data({'error': str(exc), 'session_id': session_id})

                yield SSEFormatter.format_data({'done': True, 'session_id': session_id})

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
            # 返回完整技能信息（含描述、版本、使用次数）而非仅 ID 列表。
            # 使用 visible_skill_ids() 过滤掉 hidden=True 的已弃用/内部技能，
            # 避免"os 密码"等无用技能出现在客户端列表中。
            skills_info = []
            for sid in _skills.visible_skill_ids():
                skill = _skills.get(sid)
                if skill is None:
                    continue
                skills_info.append({
                    "id": skill.id,
                    "title": skill.title,
                    "description": skill.description,
                    "version": skill.version,
                    "uses": skill.uses,
                    "last_used": skill.last_used,
                    "directory": skill.directory,
                })
            return {"skills": skills_info}

        # ── 角色 CRUD ──────────────────────────────────────────
        def _get_role_store():
            """从 MemoryPlugin 获取 RoleStore。"""
            if _memory is None:
                return None
            return getattr(_memory, "roles", None)

        @app.get("/api/roles")
        async def roles_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            store = _get_role_store()
            if store is None:
                raise HTTPException(503, "role store not available")
            return {"roles": store.list_all()}

        @app.post("/api/roles")
        async def role_create(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            store = _get_role_store()
            if store is None:
                raise HTTPException(503, "role store not available")
            name = (body.get("name") or "").strip()
            if not name:
                raise HTTPException(400, "name is required")
            try:
                role = store.create(
                    name=name,
                    description=body.get("description", ""),
                    system_prompt_override=body.get("system_prompt_override", ""),
                    icon=body.get("icon", "🤖"),
                    color=body.get("color", "#6750A4"),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    raise HTTPException(409, f"role '{name}' already exists")
                raise HTTPException(500, str(exc))
            return {"role": role}

        # 静态路由必须在动态路由 /api/roles/{role_id} 之前注册，
        # 否则 "active"/"deactivate" 会被 {role_id} 匹配并触发 422 错误。
        @app.get("/api/roles/active")
        async def role_get_active(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            store = _get_role_store()
            if store is None:
                raise HTTPException(503, "role store not available")
            role = store.get_active()
            return {"role": role}

        @app.post("/api/roles/deactivate")
        async def role_deactivate(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            store = _get_role_store()
            if store is None:
                raise HTTPException(503, "role store not available")
            store.deactivate_all()
            return {"deactivated": True}

        @app.get("/api/roles/{role_id}")
        async def role_get(role_id: int, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            store = _get_role_store()
            if store is None:
                raise HTTPException(503, "role store not available")
            role = store.get(role_id)
            if role is None:
                raise HTTPException(404, "role not found")
            return {"role": role}

        @app.put("/api/roles/{role_id}")
        async def role_update(role_id: int, body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            store = _get_role_store()
            if store is None:
                raise HTTPException(503, "role store not available")
            if store.get(role_id) is None:
                raise HTTPException(404, "role not found")
            try:
                role = store.update(role_id, **body)
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    raise HTTPException(409, f"role name already exists")
                raise HTTPException(500, str(exc))
            return {"role": role}

        @app.delete("/api/roles/{role_id}")
        async def role_delete(role_id: int, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            store = _get_role_store()
            if store is None:
                raise HTTPException(503, "role store not available")
            try:
                if not store.delete(role_id):
                    raise HTTPException(404, "role not found")
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {"deleted": True}

        @app.post("/api/roles/{role_id}/activate")
        async def role_activate(role_id: int, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            store = _get_role_store()
            if store is None:
                raise HTTPException(503, "role store not available")
            if not store.activate(role_id):
                raise HTTPException(404, "role not found")
            return {"activated": role_id}

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
            """Uninstall a skill package from the target directory.

            支持两种调用方式：
            1. 传 target_dir 为技能**自身目录路径**（即 skills list 返回的 directory
               字段，如 /data/skills/builtin/web_search）— 直接删除该目录。
            2. 传 target_dir 为**父目录**（如 /data/skills/marketplace）— 删除
               target_dir/name（兼容旧版 marketplace 行为）。
            """
            auth(x_api_key)

            from pathlib import Path as _Path
            import shutil as _shutil

            data_dir = _ctx.config.get("agent", {}).get("data_dir", "./data") if _ctx else "./data"
            allowed_base = os.path.realpath(os.path.join(data_dir, "skills"))

            # 校验并解析目标目录
            if not target_dir:
                # 未指定时回退到 marketplace 目录 + name
                candidate = os.path.join(allowed_base, "marketplace", name)
            else:
                # target_dir 可能是技能自身目录，也可能是父目录
                self_dir = os.path.realpath(target_dir)
                parent_with_name = os.path.realpath(os.path.join(target_dir, name))
                # 优先判断 target_dir/name 是否存在且在 skills 下（旧版兼容）
                if is_path_within(parent_with_name, allowed_base) and _Path(parent_with_name).exists():
                    candidate = parent_with_name
                elif is_path_within(self_dir, allowed_base) and _Path(self_dir).exists():
                    # target_dir 就是技能自身目录
                    candidate = self_dir
                else:
                    raise HTTPException(403, "target_dir must be within skills directory")

            candidate_path = _Path(candidate)
            # 安全：禁止删除 skills 根目录本身
            if os.path.realpath(candidate) == allowed_base:
                raise HTTPException(403, "cannot delete skills root directory")
            if not candidate_path.exists():
                raise HTTPException(404, _("skill_not_found", name=name))

            try:
                _shutil.rmtree(candidate_path)
                logger.info("Uninstalled skill: %s -> %s", name, candidate)
                # 从 SkillsRegistry 中卸载（运行时移除）
                if _skills is not None:
                    try:
                        _skills.unregister(name)
                    except Exception:
                        pass
                return {"uninstalled": True, "name": name, "directory": candidate}
            except Exception as exc:
                logger.error("Uninstall skill failed: %s", exc, exc_info=True)
                raise HTTPException(500, f"uninstall failed: {exc}")

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

        @app.post("/api/sessions/batch_delete")
        async def batch_delete_sessions(
            body: dict,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """批量删除会话。body: {"session_ids": ["id1", "id2", ...]}"""
            auth(x_api_key)
            store = _ctx.session_store if _ctx else None
            if store is None:
                raise HTTPException(503, _("session_store_not_available"))
            session_ids = body.get("session_ids") or []
            if not isinstance(session_ids, list):
                raise HTTPException(400, "session_ids must be a list")
            deleted_ids = []
            failed_ids = []
            for sid in session_ids:
                try:
                    if store.delete_session(str(sid)):
                        deleted_ids.append(sid)
                    else:
                        failed_ids.append(sid)
                except Exception:
                    failed_ids.append(sid)
            return {"deleted": deleted_ids, "failed": failed_ids,
                    "deleted_count": len(deleted_ids), "failed_count": len(failed_ids)}

        @app.get("/api/sessions/{session_id}/messages")
        async def get_session_messages(
            session_id: str,
            limit: int = 100,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """获取指定会话的消息列表（用于状态页查看会话详情）。"""
            auth(x_api_key)
            store = _ctx.session_store if _ctx else None
            if store is None:
                raise HTTPException(503, _("session_store_not_available"))
            session = store.get_session(session_id)
            if session is None:
                raise HTTPException(404, _("session_not_found", session_id=session_id))
            messages = store.get_messages(session_id, limit=limit) if hasattr(store, "get_messages") else []
            return {"session": session, "messages": messages}

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
            """修改配置项。body: {"key": "模型", "value": "gpt-4o"}

            问题11 修复：与 /api/config 保持一致 — 修改 LLM 相关配置后
            热重载 LLMProvider，让 base_urls / primary_model / api_keys
            等改动即时生效，无需重启服务。
            """
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
                        # 问题11：LLM 相关配置改动后热重载 LLMProvider，
                        # 与 /api/config 行为保持一致。注意 _register_settings_routes
                        # 没有 _llm 参数，必须从 _ctx.get_plugin("llm") 获取。
                        if path.startswith("llm."):
                            _llm_inst = _ctx.get_plugin("llm")
                            if _llm_inst is not None:
                                try:
                                    import asyncio
                                    async def _resync_llm():
                                        await _llm_inst.setup(_ctx)
                                    try:
                                        # 问题1 修复：同步等待 setup 完成
                                        await _resync_llm()
                                    except RuntimeError:
                                        asyncio.run(_resync_llm())
                                    logger.info("LLM provider re-synced after /api/settings update (%s)", path)
                                except Exception as exc:
                                    logger.warning("LLM re-sync failed after /api/settings update: %s", exc)
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

    def _register_webhook_routes(self, app, auth, _ctx):
        """Webhook 管理 CRUD + 测试触发端点。

        对接 core.webhook_trigger 的 register/unregister/trigger 接口，
        让"事件→HTTP webhook"链路可通过 REST API 配置。
        """
        from dataclasses import asdict
        from fastapi import Header, HTTPException

        def _webhook_to_dict(w):
            """Serialize a Webhook to a dict, masking sensitive credentials."""
            d = asdict(w)
            # 安全：API key / secret 不明文回传，避免通过 GET /api/webhooks 泄露
            if d.get("api_key"):
                d["api_key"] = _mask_api_key(d["api_key"])
            if d.get("secret_key"):
                d["secret_key"] = "***"
            return d

        @app.get("/api/webhooks")
        async def webhooks_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """列出所有已注册的 webhook。"""
            auth(x_api_key)
            from core.webhook_trigger import get_webhook_trigger
            trigger = get_webhook_trigger()
            return {
                "webhooks": [_webhook_to_dict(w) for w in trigger.list_webhooks()],
                "stats": trigger.get_stats(),
            }

        @app.post("/api/webhooks")
        async def webhook_create(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """注册新 webhook。body 字段对应 core.webhook_trigger.Webhook。

            必填: url。可选: name, method, auth_type, api_key, secret_key,
            payload_template, event_filter, rate_limit, enabled, max_retries,
            retry_delay。
            """
            auth(x_api_key)
            from core.webhook_trigger import get_webhook_trigger, Webhook
            trigger = get_webhook_trigger()
            url = (body.get("url") or "").strip()
            if not url:
                raise HTTPException(400, "url is required")
            if not (url.startswith("http://") or url.startswith("https://")):
                raise HTTPException(400, "url must be a http(s) URL")
            try:
                wh = Webhook(
                    name=body.get("name", ""),
                    url=url,
                    method=body.get("method", "POST"),
                    auth_type=body.get("auth_type", "none"),
                    api_key=body.get("api_key", ""),
                    secret_key=body.get("secret_key", ""),
                    payload_template=body.get("payload_template", "{}"),
                    event_filter=body.get("event_filter", ""),
                    rate_limit=int(body.get("rate_limit", 10)),
                    enabled=bool(body.get("enabled", True)),
                    max_retries=int(body.get("max_retries", 3)),
                    retry_delay=float(body.get("retry_delay", 1.0)),
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"invalid webhook config: {exc}")
            trigger.register(wh)
            logger.info("webhook registered via API: %s (%s)", wh.id, wh.url)
            return {"registered": True, "webhook": _webhook_to_dict(wh)}

        @app.delete("/api/webhooks/{webhook_id}")
        async def webhook_delete(webhook_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """删除指定 webhook。"""
            auth(x_api_key)
            from core.webhook_trigger import get_webhook_trigger
            trigger = get_webhook_trigger()
            ok = trigger.unregister(webhook_id)
            if not ok:
                raise HTTPException(404, "webhook not found")
            return {"deleted": True, "webhook_id": webhook_id}

        @app.post("/api/webhooks/{webhook_id}/test")
        async def webhook_test(
            webhook_id: str,
            body: dict = None,
            x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        ):
            """测试触发一个 webhook。可选 body.event_data 自定义测试数据。"""
            auth(x_api_key)
            from core.webhook_trigger import get_webhook_trigger
            trigger = get_webhook_trigger()
            wh = trigger.get_webhook(webhook_id)
            if wh is None:
                raise HTTPException(404, "webhook not found")
            event_data = (body or {}).get("event_data") or {
                "test": True,
                "timestamp": time.time(),
            }
            success = await trigger.trigger(webhook_id, event_data)
            return {
                "success": success,
                "webhook_id": webhook_id,
                "last_error": wh.last_error,
                "success_count": wh.success_count,
                "failure_count": wh.failure_count,
            }

    def _register_backup_routes(self, app, auth, _ctx, _llm, _memory, _bus, _skills, _app_instance, _memory_plugin):
        """注册数据备份/导出/导入端点。

        core/backup_export.py 的 DataExporter/DataImporter 完整实现了
        导出（zip/tar.gz/json）与导入逻辑，但之前无任何 API 端点调用。
        这里暴露 3 个端点让该模块真正生效。
        """
        from fastapi import Header, HTTPException
        from core.backup_export import (
            DataExporter,
            DataImporter,
            DataType,
            ExportFormat,
        )

        @app.post("/api/backup/export")
        async def backup_export(body: dict = None, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """导出全部 agent 数据到备份归档。

            body 可选字段：
              - format: "zip" | "tar.gz" | "json"（默认 zip）
              - output_path: 自定义输出路径（不传则生成临时文件）
              - include_config: 是否包含配置（默认 true）
            """
            auth(x_api_key)
            if _ctx is None:
                raise HTTPException(503, "Context not initialized")

            body = body or {}
            data_dir = _ctx.config.get("agent", {}).get("data_dir", "./data")
            format_str = str(body.get("format", "zip")).lower()
            try:
                fmt = ExportFormat(format_str)
            except ValueError:
                raise HTTPException(400, f"Invalid format: {format_str}")

            output_path = body.get("output_path")
            if not output_path:
                import tempfile
                ext = {
                    ExportFormat.ZIP: "zip",
                    ExportFormat.TAR_GZ: "tar.gz",
                    ExportFormat.JSON: "json",
                }.get(fmt, "zip")
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
                tmp.close()
                output_path = tmp.name

            include_config = bool(body.get("include_config", True))
            try:
                exporter = DataExporter(data_dir=data_dir)
                result = exporter.export_all(
                    output_path=output_path,
                    format=fmt,
                    include_config=include_config,
                )
            except Exception as exc:
                logger.error("Backup export failed: %s", exc, exc_info=True)
                raise HTTPException(500, f"Export failed: {exc}")

            return {
                "success": result.success,
                "format": result.format,
                "file_path": result.file_path,
                "size_bytes": result.size_bytes,
                "items_exported": result.items_exported,
                "duration_seconds": result.duration_seconds,
                "error": result.error,
            }

        @app.post("/api/backup/import")
        async def backup_import(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """从备份归档导入 agent 数据。

            body 字段：
              - file_path: 备份文件路径（必填）
              - merge: 是否合并而非覆盖（默认 true）
            """
            auth(x_api_key)
            if _ctx is None:
                raise HTTPException(503, "Context not initialized")

            file_path = body.get("file_path")
            if not file_path:
                raise HTTPException(400, "file_path is required")

            merge = bool(body.get("merge", True))
            data_dir = _ctx.config.get("agent", {}).get("data_dir", "./data")
            try:
                importer = DataImporter(data_dir=data_dir)
                result = importer.import_from_file(file_path=file_path, merge=merge)
            except Exception as exc:
                logger.error("Backup import failed: %s", exc, exc_info=True)
                raise HTTPException(500, f"Import failed: {exc}")

            return {
                "success": result.success,
                "items_imported": result.items_imported,
                "duration_seconds": result.duration_seconds,
                "error": result.error,
            }

        @app.get("/api/backup/list")
        async def backup_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """列出可导出的数据类型与支持的导出格式。"""
            auth(x_api_key)
            return {
                "data_types": [t.value for t in DataType],
                "formats": [f.value for f in ExportFormat],
            }

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
            allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH"],
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

            # 修复：验证 IP 格式防止恶意 header 撑爆 _rate_buckets 字典
            # 合法 IP 长度不超过 45 字符（IPv6 全格式），只允许数字、字母、.:-
            # 非法 IP 归一为 "unknown"，与无 IP 的请求共享 bucket
            if not ip or len(ip) > 45 or not all(c.isalnum() or c in '.:-' for c in ip):
                ip = "unknown"

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
            # 修复：用 startswith 兼容尾斜杠和反向代理 rewrite
            # 例如 /api/chat/、/api/chat/stream/、/chat（rewritten）都能匹配
            path = request.url.path.rstrip("/")
            if path in ("/api/chat", "/api/chat/stream"):
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
            cfg_path = _resolve_config_path()
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

        # ---------------------------------------------------------------- Webhook Management
        self._register_webhook_routes(app, auth, _ctx)

        # ---------------------------------------------------------------- Backup & Export
        self._register_backup_routes(app, auth, _ctx, _llm, _memory, _bus, _skills, _app_instance, _memory_plugin)

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
