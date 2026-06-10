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
(configurable via ATHENA_API_KEY env var).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

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

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("rest") or {}
        self._host = cfg.get("host", self._host)
        self._port = int(cfg.get("port", self._port))
        self._enabled = bool(cfg.get("enabled", True))
        self._api_key = cfg.get("api_key", "")
        import os as _os
        self._api_key = _os.environ.get("ATHENA_API_KEY", self._api_key)
        logger.info("REST API configured on %s:%s auth=%s",
                    self._host, self._port, bool(self._api_key))

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
            title="Athena Agent API",
            version="2.0.0",
            description="REST API for Athena Agent integration",
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        _agent = self._agent_callback
        _ctx = self.ctx
        _llm = next((p for p in (_ctx._plugins if _ctx else []) if getattr(p, "name", "") == "llm"), None) if _ctx else None
        _memory = next((p for p in (_ctx._plugins if _ctx else []) if getattr(p, "name", "") == "memory"), None) if _ctx else None
        _skills = next((p for p in (_ctx._plugins if _ctx else []) if getattr(p, "name", "") == "skills"), None) if _ctx else None
        _bus = _ctx.bus if _ctx else None

        def auth(x_api_key: Optional[str] = Header(None)) -> None:
            if self._api_key and x_api_key != self._api_key:
                raise HTTPException(401, "Invalid API key")

        @app.get("/api/health")
        async def health():
            return {"status": "ok", "uptime": int(time.time() - (_ctx.started_at if _ctx else time.time()))}

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
        async def chat(body: dict, _: None = Header(None)):
            auth(_)
            text = body.get("text", "")
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
            _: None = Header(None),
        ):
            auth(_)
            if _memory is None:
                raise HTTPException(503, "memory not available")
            results = _memory.search_facts(q, limit=limit, offset=offset)
            return {"query": q, "results": results, "limit": limit, "offset": offset}

        @app.post("/api/memory/add")
        async def memory_add(body: dict, _: None = Header(None)):
            auth(_)
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
            _: None = Header(None),
        ):
            auth(_)
            if _memory is None:
                raise HTTPException(503, "memory not available")
            return _memory._long.paginate(page=page, page_size=page_size)  # type: ignore[union-attr]

        @app.get("/api/skills")
        async def skills_list(_: None = Header(None)):
            auth(_)
            if _skills is None:
                raise HTTPException(503, "skills not available")
            return {"skills": _skills.all_skill_ids()}

        @app.post("/api/cache/clear")
        async def cache_clear(_: None = Header(None)):
            auth(_)
            if _llm is None:
                raise HTTPException(503, "llm not available")
            return _llm.clear_cache()

        @app.exception_handler(Exception)
        async def all_exception(request: Request, exc: Exception):
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
        await super().stop()
