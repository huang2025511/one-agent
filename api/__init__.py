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
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        # Store the app instance for thinking access
        if hasattr(cb, "__self__"):
            self._app_instance = cb.__self__

    async def start(self) -> None:
        if not self._enabled:
            return
        try:
            from fastapi import FastAPI, HTTPException, Header, Request, UploadFile
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
            if request.url.path in ("/api/chat", "/api/chat/stream"):
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
        _app_instance: Any = getattr(self, "_app_instance", None)
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

        # ── Dashboard ──────────────────────────────────────────────────
        @app.get("/dashboard")
        async def dashboard():
            """Serve the monitoring dashboard."""
            from fastapi.responses import HTMLResponse
            from api.dashboard import get_dashboard_html
            return HTMLResponse(content=get_dashboard_html())

        # ── Dashboard API endpoints ────────────────────────────────────
        @app.get("/api/costs/daily")
        async def costs_daily(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get today's cost and budget."""
            auth(x_api_key)
            _cost_tracker = getattr(_ctx, "cost_tracker", None) if _ctx else None
            if _cost_tracker is None:
                return {"cost": 0.0, "budget": 0.0, "remaining": 0.0}
            today_cost = _cost_tracker.cost_today()
            daily_budget = getattr(_cost_tracker, "_daily_budget", 1.0)
            return {
                "cost": today_cost,
                "budget": daily_budget,
                "remaining": max(0, daily_budget - today_cost),
            }

        @app.get("/api/costs/monthly")
        async def costs_monthly(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get this month's cost."""
            auth(x_api_key)
            _cost_tracker = getattr(_ctx, "cost_tracker", None) if _ctx else None
            if _cost_tracker is None:
                return {"cost": 0.0}
            return {"cost": _cost_tracker.cost_this_month()}

        @app.get("/api/sessions/list")
        async def sessions_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List recent sessions."""
            auth(x_api_key)
            _session_store = getattr(_ctx, "session_store", None) if _ctx else None
            if _session_store is None:
                return {"sessions": []}
            sessions = _session_store.list_sessions(limit=20)
            return {"sessions": sessions}

        @app.get("/api/approvals/pending")
        async def approvals_pending(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List pending approval requests."""
            auth(x_api_key)
            _approval_mgr = getattr(_ctx, "approval_manager", None) if _ctx else None
            if _approval_mgr is None:
                return {"requests": []}
            pending = _approval_mgr.list_pending()
            return {"requests": pending}

        @app.post("/api/sessions/{session_id}/fork")
        async def fork_session(session_id: str, body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Fork a session at a specific message index."""
            auth(x_api_key)
            _session_store = getattr(_ctx, "session_store", None) if _ctx else None
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
            _session_store = getattr(_ctx, "session_store", None) if _ctx else None
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

        @app.get("/api/stats")
        async def stats():
            """System statistics for dashboard."""
            _session_store = getattr(_ctx, "session_store", None) if _ctx else None
            _memory_plugin = _ctx.get_plugin("memory") if _ctx else None
            
            # Get session statistics
            sessions_data = {}
            if _session_store:
                try:
                    all_sessions = _session_store.list_sessions(limit=1000)
                    active_count = sum(1 for s in all_sessions if s.get("status") == "active")
                    total_messages = sum(s.get("message_count", 0) for s in all_sessions)
                    sessions_data = {
                        "active": active_count,
                        "total": len(all_sessions)
                    }
                except Exception:
                    sessions_data = {"active": 0, "total": 0}
            
            # Get knowledge graph entity count
            kg_data = {}
            if _memory_plugin and hasattr(_memory_plugin, "kg"):
                try:
                    kg = _memory_plugin.kg
                    entity_count = len(kg.entities) if hasattr(kg, "entities") else 0
                    kg_data = {"entities": entity_count}
                except Exception:
                    kg_data = {"entities": 0}
            
            return {
                "uptime_seconds": _ctx.uptime() if _ctx else 0,
                "bus_metrics": _bus.metrics() if _bus else {},
                "llm_stats": _llm.stats() if _llm else {},
                "memory_stats": _memory.stats() if _memory else {},
                "skills_count": len(_skills.all_skill_ids()) if _skills else 0,
                # Dashboard-specific fields
                "sessions": sessions_data,
                "messages": {"total": sessions_data.get("total", 0)},
                "knowledge_graph": kg_data,
                "skills": {"installed": len(_skills.all_skill_ids()) if _skills else 0},
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
            
            # Auto-detect language from user input
            if text:
                from i18n import detect_language, set_language, get_language
                detected_lang = detect_language(text)
                current_lang = get_language()
                if detected_lang != current_lang:
                    set_language(detected_lang)
                    logger.info(f"Auto-detected language: {detected_lang} from API request")
                    # Persist language preference to config
                    if _ctx:
                        try:
                            config = _ctx.config
                            if config.get("agent", {}).get("language") != detected_lang:
                                config.setdefault("agent", {})["language"] = detected_lang
                                from skills import _save_config
                                _save_config(config)
                        except Exception as exc:
                            logger.warning("failed to persist language: %s", exc)
            
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
            session_id = body.get("session_id", uuid.uuid4().hex[:12])
            model = body.get("model")
            temperature = body.get("temperature")
            max_tokens = body.get("max_tokens")
            tools = body.get("tools")

            # Auto-detect language from user input
            if text:
                from i18n import detect_language, set_language, get_language
                detected_lang = detect_language(text)
                current_lang = get_language()
                if detected_lang != current_lang:
                    set_language(detected_lang)

            if _llm is None:
                raise HTTPException(503, _("llm_not_available"))

            from fastapi.responses import StreamingResponse

            async def event_generator():
                # Build messages list
                msgs: List[Dict[str, Any]] = [{"role": "user", "content": text}]
                if body.get("system"):
                    msgs.insert(0, {"role": "system", "content": body["system"]})
                yield f"data: {json.dumps({'status': 'thinking', 'session_id': session_id})}\n\n"
                async for chunk in _llm.chat_completion_stream(
                    messages=msgs,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                ):
                    yield f"data: {json.dumps(chunk)}\n\n"
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

        # ---------------------------------------------------------------- Marketplace endpoints
        @app.get("/api/marketplace")
        async def list_marketplace(query: str = "", x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Discover available skill packages in the marketplace."""
            auth(x_api_key)
            mp = getattr(_ctx, "marketplace", None) if _ctx else None
            if mp is None:
                raise HTTPException(503, _("marketplace_not_available"))
            return {"packages": mp.discover(query)}

        @app.post("/api/marketplace/publish")
        async def publish_skill(dirpath: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Publish a local skill directory to the marketplace."""
            auth(x_api_key)
            mp = getattr(_ctx, "marketplace", None) if _ctx else None
            if mp is None:
                raise HTTPException(503, _("marketplace_not_available"))
            pkg = mp.publish(dirpath)
            if pkg is None:
                raise HTTPException(400, _("invalid_skill_package", path=dirpath))
            return {"published": True, "package": pkg.to_dict()}

        @app.post("/api/marketplace/install")
        async def install_skill(name: str, target_dir: str = "", x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Install a skill package from the marketplace."""
            auth(x_api_key)
            mp = getattr(_ctx, "marketplace", None) if _ctx else None
            if mp is None:
                raise HTTPException(503, _("marketplace_not_available"))
            if not target_dir:
                target_dir = os.path.join(_ctx.config.get("agent", {}).get("data_dir", "./data"), "skills", "marketplace")
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
            mp = getattr(_ctx, "marketplace", None) if _ctx else None
            if mp is None:
                raise HTTPException(503, _("marketplace_not_available"))
            if not target_dir:
                target_dir = os.path.join(_ctx.config.get("agent", {}).get("data_dir", "./data"), "skills", "marketplace")
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

        # ---------------------------------------------------------------- Self-improvement endpoints
        @app.get("/api/improvements")
        async def get_improvements(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get self-improvement stats and patterns."""
            auth(x_api_key)
            improver = getattr(_ctx, "self_improver", None) if _ctx else None
            if improver is None:
                raise HTTPException(503, _("improvement_not_available"))
            stats = improver.get_stats()
            improvements = improver.get_improvements()
            return {**stats, "applied_improvements": improvements}

        @app.get("/api/improvements/failures")
        async def get_failures(limit: int = 50, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Get recent failure cases."""
            auth(x_api_key)
            improver = getattr(_ctx, "self_improver", None) if _ctx else None
            if improver is None:
                raise HTTPException(503, _("improvement_not_available"))
            failures = improver.get_failures(limit=limit)
            return {"failures": failures, "limit": limit}

        # ---------------------------------------------------------------- Cost tracking endpoints
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

        # ---------------------------------------------------------------- Session endpoints
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

        # ---------------------------------------------------------------- Settings
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

        # ---------------------------------------------------------------- Document RAG
        @app.post("/api/documents/ingest")
        async def ingest_document(file: UploadFile = None, path: str = None,
                                  x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            from skills import _doc_store
            if file is not None:
                import tempfile
                import shutil
                with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
                    shutil.copyfileobj(file.file, tmp)
                    tmp_path = tmp.name
                try:
                    count = _doc_store.ingest_file(tmp_path)
                finally:
                    os.unlink(tmp_path)
                return {"ingested": True, "name": file.filename, "chunks": count}
            if path:
                count = _doc_store.ingest_file(path)
                return {"ingested": True, "name": Path(path).name, "chunks": count}
            raise HTTPException(400, _("need_file_or_path"))

        @app.get("/api/documents")
        async def list_documents(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            from skills import _doc_store
            docs = _doc_store.list_documents()
            return {"documents": docs}

        @app.get("/api/documents/search")
        async def search_documents(q: str, limit: int = 5,
                                   x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            from skills import _doc_store
            results = _doc_store.search(q, limit=limit)
            return {"query": q, "results": results, "limit": limit}

        @app.delete("/api/documents/{name}")
        async def delete_document(name: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            auth(x_api_key)
            from skills import _doc_store
            deleted = _doc_store.delete_document(name)
            if not deleted:
                raise HTTPException(404, _("document_not_found", name=name))
            return {"deleted": True, "name": name}

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

        # ---------------------------------------------------------------- Approval (Human-in-the-Loop)
        @app.get("/api/approvals/pending")
        async def list_pending_approvals(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """List pending approval requests."""
            auth(x_api_key)
            _approval_mgr = getattr(_ctx, "approval_manager", None) if _ctx else None
            if _approval_mgr is None:
                return {"pending": []}
            return {"pending": _approval_mgr.get_pending()}

        @app.post("/api/approvals/{request_id}/approve")
        async def approve_request(request_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """Approve a pending request."""
            auth(x_api_key)
            _approval_mgr = getattr(_ctx, "approval_manager", None) if _ctx else None
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
            _approval_mgr = getattr(_ctx, "approval_manager", None) if _ctx else None
            if _approval_mgr is None:
                raise HTTPException(503, _("approval_manager_not_available"))
            ok = _approval_mgr.deny(request_id)
            if not ok:
                raise HTTPException(404, _("approval_request_not_found", request_id=request_id))
            return {"approved": False, "request_id": request_id}

        # ── MCP (Model Context Protocol) 端点 ──────────────────────────
        @app.get("/api/mcp/tools")
        async def mcp_list_tools(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """列出所有可用的 MCP 工具"""
            auth(x_api_key)
            _mcp_client = getattr(_ctx, "mcp_client", None) if _ctx else None
            if _mcp_client is None:
                raise HTTPException(503, _("mcp_client_not_available"))
            tools = _mcp_client.list_tools()
            return {"tools": tools}

        @app.post("/api/mcp/call")
        async def mcp_call_tool(body: dict, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
            """调用 MCP 工具"""
            auth(x_api_key)
            _mcp_client = getattr(_ctx, "mcp_client", None) if _ctx else None
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
            _mcp_client = getattr(_ctx, "mcp_client", None) if _ctx else None
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
            _mcp_client = getattr(_ctx, "mcp_client", None) if _ctx else None
            if _mcp_client is None:
                raise HTTPException(503, _("mcp_client_not_available"))
            await _mcp_client.remove_server(server_name)
            return {"success": True, "server": server_name}

        @app.exception_handler(Exception)
        async def all_exception(request: Request, exc: Exception):
            from starlette.exceptions import HTTPException as StarletteHTTPException
            if isinstance(exc, StarletteHTTPException):
                # Return proper HTTP response instead of re-raising so uvicorn
                # doesn't interfere with the exception propagation.
                return JSONResponse(
                    {"detail": exc.detail},
                    status_code=getattr(exc, "status_code", 500),
                )
            logger.exception("api error: %s", exc)
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
