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

import logging
import time
import uuid
from typing import Dict, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)


def _check_auth(api_key: Optional[str], required_key: str) -> bool:
    if not required_key:
        return True  # Auth disabled if no key configured
    return api_key == required_key


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
                return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
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
                            {"error": f"request body too large ({cl} > {self._max_chat_bytes})"},
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
                raise HTTPException(401, "Invalid API key")

        @app.get("/api/health")
        async def health():
            if _ctx is None:
                return {"status": "not_ready", "uptime": 0}
            return {"status": "ok", "uptime": int(time.time() - _ctx.started_at)}

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
                raise HTTPException(503, "agent not ready")
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
                raise HTTPException(503, "memory not available")
            results = _memory.search_facts(q, limit=limit, offset=offset)
            return {"query": q, "results": results, "limit": limit, "offset": offset}

        @app.post("/api/memory/add")
        async def memory_add(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            if _memory is None:
                raise HTTPException(503, "memory not available")
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
                raise HTTPException(503, "memory not available")
            return _memory.paginate_facts(page=page, page_size=page_size)

        @app.get("/api/skills")
        async def skills_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            if _skills is None:
                raise HTTPException(503, "skills not available")
            return {"skills": _skills.all_skill_ids()}

        @app.post("/api/cache/clear")
        async def cache_clear(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            if _llm is None:
                raise HTTPException(503, "llm not available")
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
            return {"error": f"unknown key: {key}"}

        @app.post("/api/settings")
        async def settings_set(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """修改配置项。body: {"key": "模型", "value": "gpt-4o"}"""
            auth(x_api_key)
            from skills import _SETTING_ALIASES, _set_nested, _parse_value, _SENSITIVE_KEYS, _is_sensitive_write_allowed, _save_config
            key = body.get("key", "")
            value = body.get("value")
            if not key or value is None:
                raise HTTPException(400, "need key and value")
            for alias, (path, vtype) in _SETTING_ALIASES.items():
                if alias == key or path == key:
                    if any(sk in path for sk in _SENSITIVE_KEYS):
                        if not _is_sensitive_write_allowed(_ctx.config if _ctx else {}):
                            raise HTTPException(403, f"{alias} is sensitive — enable security.allow_sensitive_chat_settings to modify via API")
                    parsed = _parse_value(str(value), vtype)
                    if parsed is None:
                        raise HTTPException(400, f"cannot parse value for type {vtype.__name__}")
                    if _ctx:
                        _set_nested(_ctx.config, path, parsed)
                        _save_config(_ctx.config)
                    return {"alias": alias, "path": path, "value": parsed, "saved": True}
            raise HTTPException(404, f"unknown key: {key}")

        @app.exception_handler(Exception)
        async def all_exception(request: Request, exc: Exception):
            from starlette.exceptions import HTTPException as StarletteHTTPException
            if isinstance(exc, StarletteHTTPException):
                raise exc
            logger.exception("api error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

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
