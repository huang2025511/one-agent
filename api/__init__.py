"""REST API server — FastAPI backend for external integrations.

Exposes:
  POST /api/chat           — send a message, get a reply
  GET  /api/memory/search  — query long-term memory
  POST /api/memory/add     — add a fact to memory
  GET  /api/memory/page    — paginated memory list
  GET  /api/skills         — list all skills
  POST /api/skills/install — install a skill from URL / GitHub
  GET  /api/stats          — system statistics
  GET  /api/metrics        — bus + LLM + memory metrics
  POST /api/cache/clear    — clear LLM response cache
  GET  /api/health         — health check

All endpoints return JSON.  Authentication via X-API-Key header
(configurable via ONE_AGENT_API_KEY env var).
"""

from __future__ import annotations

import hmac
import logging
import time
import uuid
from typing import Dict, List, Optional

from core.plugin import Plugin
from i18n import _

logger = logging.getLogger(__name__)


def _check_auth(api_key: Optional[str], required_key: str) -> bool:
    """Compare API keys using constant-time comparison to prevent timing attacks."""
    if not required_key:
        return True  # Auth disabled if no key configured
    # Use hmac.compare_digest for constant-time comparison
    return hmac.compare_digest(api_key or "", required_key)


class RESTAPIGateway(Plugin):
    """FastAPI REST server plugin."""

    name = "gateway_rest"

    def __init__(self) -> None:
        super().__init__()
        self._host = "0.0.0.0"
        self._port = 18792
        self._enabled = True
        self._task = None
        self._app = None
        self._api_key = ""
        self._agent_callback = None
        # Per-IP rate-limit buckets — must live on the instance so they
        # survive any restart of the underlying FastAPI app (e.g. dev
        # mode auto-reload).  Format: {ip: [timestamp, ...]}
        self._rate_buckets: Dict[str, list] = {}
        # Default rate limit (overridden in setup() from config)
        self._rate_limit = 60
        # Max accepted chat request body size (bytes) — protects the
        # server from a single client streaming gigabytes of input.
        self._max_chat_bytes = 64 * 1024  # 64 KB
        # CORS: default to wildcard in dev; setup() reads config to
        # restrict to a real origin list for production deployments.
        self._cors_origins: List[str] = ["*"]

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("rest") or {}
        self._host = cfg.get("host", self._host)
        self._port = int(cfg.get("port", self._port))
        self._enabled = bool(cfg.get("enabled", True))
        self._api_key = cfg.get("api_key", "")
        self._rate_limit = int(cfg.get("rate_limit_per_minute", self._rate_limit))
        self._max_chat_bytes = int(cfg.get("max_chat_bytes", self._max_chat_bytes))
        import os as _os
        self._api_key = _os.environ.get("ONE_AGENT_API_KEY", self._api_key)
        # CORS: restrict to configured origins in production.  Falls back
        # to a wildcard when no origins are configured (developer mode).
        self._cors_origins = cfg.get("cors_origins") or ["*"]
        logger.info("REST API configured on %s:%s auth=%s rate_limit=%d/min max_chat=%dB cors=%s",
                    self._host, self._port, bool(self._api_key),
                    self._rate_limit, self._max_chat_bytes, self._cors_origins)

    def bind_callback(self, cb) -> None:
        self._agent_callback = cb

    async def start(self) -> None:
        if not self._enabled:
            return
        try:
            from fastapi import FastAPI, HTTPException, Header, Request
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
            ip = request.client.host if request.client else "unknown"
            now = time.time()
            bucket = self._rate_buckets.setdefault(ip, [])
            # evict entries older than 60s
            bucket[:] = [t for t in bucket if now - t < 60]
            if len(bucket) >= self._rate_limit:
                return JSONResponse({"error": _("rate_limit_exceeded")}, status_code=429)
            bucket.append(now)
            return await call_next(request)

        # Reject chat requests with absurdly large bodies before FastAPI
        # even tries to parse JSON — protects the server from accidental
        # 100 MB /chat posts.
        @app.middleware("http")
        async def body_size_middleware(request, call_next):
            if request.url.path == "/api/chat":
                cl = request.headers.get("content-length")
                try:
                    if cl is not None and int(cl) > self._max_chat_bytes:
                        return JSONResponse(
                            {"error": _("request_body_too_large", size=cl, max=self._max_chat_bytes)},
                            status_code=413,
                        )
                except ValueError:
                    pass
            return await call_next(request)

        _agent = self._agent_callback
        _ctx = self.ctx
        _llm = _ctx.get_plugin("llm") if _ctx else None
        _memory = _ctx.get_plugin("memory") if _ctx else None
        _skills = _ctx.get_plugin("skills") if _ctx else None
        _bus = _ctx.bus if _ctx else None

        def auth(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> None:
            if self._api_key and x_api_key != self._api_key:
                raise HTTPException(401, _("invalid_api_key"))

        @app.get("/api/health")
        async def health():
            if _ctx is None:
                return {"status": "not_ready", "uptime": 0}
            uptime = int(time.time() - _ctx.started_at)

            # Per-component health check
            components = {}
            # LLM provider
            if _llm is not None:
                llm_s = _llm.stats()
                components["llm"] = {
                    "status": "ok" if not llm_s.get("failed") else "degraded",
                    "calls": llm_s.get("calls", 0),
                }
            else:
                components["llm"] = {"status": "unavailable"}

            # Memory
            if _memory is not None:
                components["memory"] = {"status": "ok"}
            else:
                components["memory"] = {"status": "unavailable"}

            # Event bus
            if _bus is not None:
                bus_m = _bus.metrics()
                components["bus"] = {
                    "status": "ok",
                    "queue_depth": bus_m.get("queue_depth", 0),
                    "errors": bus_m.get("errors", 0),
                }
            else:
                components["bus"] = {"status": "unavailable"}

            # Skills
            if _skills is not None:
                components["skills"] = {
                    "status": "ok",
                    "count": len(_skills.all_skill_ids()),
                }
            else:
                components["skills"] = {"status": "unavailable"}

            # Overall status: ok if at least llm + bus are ok
            all_ok = all(
                c.get("status") in ("ok", "unavailable")
                for c in components.values()
            )
            overall = "ok" if all_ok else "degraded"

            return {
                "status": overall,
                "uptime": uptime,
                "components": components,
            }

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

        @app.get("/api/stats")
        async def stats():
            return {
                "uptime_seconds": _ctx.uptime() if _ctx else 0,
                "bus_metrics": _bus.metrics() if _bus else {},
                "llm_stats": _llm.stats() if _llm else {},
                "memory_stats": _memory.stats() if _memory else {},
                "skills_count": len(_skills.all_skill_ids()) if _skills else 0,
            }

        @app.get("/api/metrics")
        async def metrics():
            return {
                "bus": _bus.metrics() if _bus else {},
                "llm": _llm.stats() if _llm else {},
                "memory": _memory.stats() if _memory else {},
            }

        @app.post("/api/chat")
        async def chat(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            text = body.get("text") or body.get("message", "")
            session_id = body.get("session_id", uuid.uuid4().hex[:12])
            if _agent is None:
                raise HTTPException(503, _("agent_not_ready"))
            reply = await _agent(text, source="api", session_id=session_id)
            return {"reply": reply, "session_id": session_id}

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

        @app.post("/api/cache/clear")
        async def cache_clear(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            if _llm is None:
                raise HTTPException(503, _("llm_not_available"))
            return _llm.clear_cache()

        @app.get("/api/settings")
        async def settings_get(key: Optional[str] = None, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """读取配置项。不传 key 则返回所有可配置项列表。"""
            auth(x_api_key)
            from skills import _SETTING_ALIASES, _get_nested, _SENSITIVE_KEYS, _is_sensitive_write_allowed
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
            return {"error": _("unknown_key", key=key)}

        @app.post("/api/settings")
        async def settings_set(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """修改配置项。body: {"key": "模型", "value": "gpt-4o"}"""
            auth(x_api_key)
            from skills import _SETTING_ALIASES, _set_nested, _parse_value, _SENSITIVE_KEYS, _is_sensitive_write_allowed, _save_config
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
                        from config_backup import ConfigBackupManager
                        import os
                        cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
                        backup_mgr = ConfigBackupManager(cfg_path)
                        backup_mgr.create_backup(reason="pre-change")
                        _set_nested(_ctx.config, path, parsed)
                        _save_config(_ctx.config)
                    return {"alias": alias, "path": path, "value": parsed, "saved": True}
            raise HTTPException(404, _("unknown_key", key=key))

        # ---------------------------------------------------------------- Config Backup
        @app.get("/api/config/backups")
        async def config_backups_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List all config backups."""
            auth(x_api_key)
            import os
            from config_backup import ConfigBackupManager
            cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
            backup_mgr = ConfigBackupManager(cfg_path)
            return {"backups": backup_mgr.list_backups()}

        @app.post("/api/config/backup")
        async def config_backup_create(body: dict = None, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Create a config backup."""
            auth(x_api_key)
            import os
            from config_backup import ConfigBackupManager
            cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
            backup_mgr = ConfigBackupManager(cfg_path)
            reason = (body or {}).get("reason", "manual")
            backup_name = backup_mgr.create_backup(reason=reason)
            if backup_name:
                return {"created": True, "filename": backup_name}
            raise HTTPException(500, _("backup_create_failed"))

        @app.post("/api/config/restore")
        async def config_restore(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Restore config from a backup."""
            auth(x_api_key)
            import os
            from config_backup import ConfigBackupManager
            cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
            backup_mgr = ConfigBackupManager(cfg_path)
            backup_name = body.get("filename")  # None means most recent
            success = backup_mgr.restore_backup(backup_name)
            if success:
                return {"restored": True, "filename": backup_name or "latest"}
            raise HTTPException(500, _("restore_failed"))

        @app.get("/api/config/backups/{filename}")
        async def config_backup_get(filename: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get backup content."""
            auth(x_api_key)
            import os
            from config_backup import ConfigBackupManager
            cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
            backup_mgr = ConfigBackupManager(cfg_path)
            content = backup_mgr.get_backup_content(filename)
            if content is not None:
                return {"filename": filename, "content": content}
            raise HTTPException(404, _("backup_not_found", filename=filename))

        @app.delete("/api/config/backups/{filename}")
        async def config_backup_delete(filename: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Delete a config backup."""
            auth(x_api_key)
            import os
            from config_backup import ConfigBackupManager
            cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
            backup_mgr = ConfigBackupManager(cfg_path)
            success = backup_mgr.delete_backup(filename)
            if success:
                return {"deleted": True, "filename": filename}
            raise HTTPException(404, _("backup_not_found", filename=filename))

        # ---------------------------------------------------------------- Alerting
        @app.get("/api/alerts/rules")
        async def alert_rules_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List all alert rules."""
            auth(x_api_key)
            _alert_mgr = getattr(_ctx, "_alert_manager", None) if _ctx else None
            if _alert_mgr is None:
                return {"rules": []}
            return {"rules": _alert_mgr.list_rules()}

        @app.post("/api/alerts/rules")
        async def alert_rule_create(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Create or update an alert rule."""
            auth(x_api_key)
            _alert_mgr = getattr(_ctx, "_alert_manager", None) if _ctx else None
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
            _alert_mgr = getattr(_ctx, "_alert_manager", None) if _ctx else None
            if _alert_mgr is None:
                raise HTTPException(503, _("alert_manager_not_available"))
            _alert_mgr.remove_rule(name)
            return {"deleted": True, "rule": name}

        @app.get("/api/alerts/history")
        async def alert_history(limit: int = 50, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get recent alert events."""
            auth(x_api_key)
            _alert_mgr = getattr(_ctx, "_alert_manager", None) if _ctx else None
            if _alert_mgr is None:
                return {"alerts": []}
            return {"alerts": _alert_mgr.list_history(limit=limit)}

        @app.exception_handler(Exception)
        async def all_exception(request: Request, exc: Exception):
            from starlette.exceptions import HTTPException as StarletteHTTPException
            if isinstance(exc, StarletteHTTPException):
                raise exc
            logger.exception("api error: %s", exc)
            # Return generic error message to avoid leaking internal details
            return JSONResponse({"error": _("internal_error")}, status_code=500)

        self._app = app
        try:
            import uvicorn
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
