"""Chat gateways — CLI, web UI, and messaging re-exports.

CLIGateway and WebGateway live here directly.  Messaging gateways
(Telegram, WeCom, DingTalk, Feishu, Discord, Slack) are imported from
``gateways.messaging`` and re-exported for plugin discovery.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from core.plugin import Plugin

from gateways.messaging import (  # noqa: F401  # re-exported for plugin discovery
    TelegramGateway,
    WeComGateway,
    DingTalkGateway,
    FeishuGateway,
    DiscordGateway,
    SlackGateway,
)
from gateways.wechat_personal import WeChatPersonalGateway  # noqa: F401

logger = logging.getLogger(__name__)


# ---------- 自然语言意图匹配 ----------
_CLI_INTENT_PATTERNS = {
    "exit": [r"退出|再见|拜拜|结束|关闭|退出程序|再见啦|bye|goodbye|see you"],
    "help": [r"帮助|怎么用|使用说明|能做什么|有什么功能|help|命令列表|功能列表|怎么操作|使用方法"],
    "status": [r"状态|运行状态|当前状态|系统状态|运行情况|status|还好吗|活着吗|运行多久"],
    "clear": [r"清屏|清除屏幕|清理屏幕|clear|刷新屏幕"],
}


def _match_cli_intent(text: str) -> Optional[str]:
    """从自然语言中匹配 CLI 意图。精准命令优先，然后模糊匹配。"""
    import re
    lower = text.lower().strip()
    exact = {
        "exit": "exit", "quit": "exit", "q": "exit",
        "help": "help", "?": "help",
        "status": "status", "clear": "clear",
    }
    if lower in exact:
        return exact[lower]
    for intent, patterns in _CLI_INTENT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, lower):
                return intent
    return None


# ------------- CLI --------------------------------------------------------
class CLIGateway(Plugin):
    """Line-based interactive terminal interface."""

    name = "gateway_cli"

    def __init__(self) -> None:
        super().__init__()
        self._prompt = "one-agent> "
        self._session_id = uuid.uuid4().hex[:12]
        self._reply_available: asyncio.Event | None = None
        self._last_reply: str = ""

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self._prompt = (ctx.config.get("gateways") or {}).get("cli", {}).get("prompt", self._prompt)
        self.bus.subscribe("turn_completed", self._on_done)
        # Subscribe to approval events for human-in-the-loop
        self.bus.subscribe("approval_needed", self._on_approval_needed)

    async def run_loop(self, send_to_agent) -> None:
        """Run the interactive REPL.  ``send_to_agent(text)`` should be an
        async function that triggers the agent pipeline."""
        from i18n import _, auto_detect_and_switch
        
        # Auto-detect language on first interaction
        print("One-Agent — 自然语言即可操作，输入 '帮助' 查看功能。")
        first_message = True
        
        while True:
            try:
                line = input(self._prompt)
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print("\n(interrupted)")
                return
            line = line.strip()
            if not line:
                continue
            
            # Auto-detect language on first user message
            if first_message:
                auto_detect_and_switch(line)
                first_message = False
            
            intent = _match_cli_intent(line)
            if intent == "exit":
                return
            if intent == "help":
                print(_("cli_help_content"))
                continue
            if intent == "status":
                print(f"session {self._session_id} up {int(time.monotonic())}s")
                continue
            if intent == "clear":
                print("\033c", end="")
                continue
            self._reply_available = asyncio.Event()
            self._last_reply = ""
            await send_to_agent(line, source="cli", session_id=self._session_id)
            try:
                await asyncio.wait_for(self._reply_available.wait(), timeout=120)
            except asyncio.TimeoutError:
                print(_("timeout"))
                continue
            print(self._last_reply)

    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None or turn.session_id != self._session_id:
            return
        self._last_reply = turn.result or f"[error: {turn.error}]"
        if self._reply_available is not None:
            self._reply_available.set()

    async def _on_approval_needed(self, event) -> None:
        """Handle approval request: prompt CLI user to approve or deny."""
        req_data = event.get("request")
        if req_data is None:
            return
        req_id = req_data.get("id", "")
        operation = req_data.get("operation", "unknown")
        details = req_data.get("details", "")
        risk_level = req_data.get("risk_level", "medium")

        # Check if request is still pending
        approval_mgr = getattr(self.ctx, "approval_manager", None) if self.ctx else None
        if approval_mgr is None:
            return
        pending = approval_mgr.get_pending()
        if not any(r["id"] == req_id for r in pending):
            return  # Already handled

        print(f"\n[⏳ 需要审批] {operation}")
        print(f"  风险等级: {risk_level}")
        print(f"  详情: {details[:200]}")
        print(f"  ID: {req_id}")

        try:
            answer = await asyncio.to_thread(
                input, "  批准执行? (y/n, 默认 120s 超时自动拒绝): "
            )
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        if answer.strip().lower().startswith("y"):
            approval_mgr.approve(req_id)
            print("  ✓ 已批准")
        else:
            approval_mgr.deny(req_id)
            print("  ✗ 已拒绝")


# ------------- Web UI (FastAPI) -------------------------------------------
class WebGateway(Plugin):
    """Tiny web UI for chatting + listing skills.  Runs on FastAPI."""

    name = "gateway_web"

    def __init__(self) -> None:
        super().__init__()
        self._host = "127.0.0.1"
        self._port = 18791
        self._enabled = True
        self._task: Optional[asyncio.Task] = None
        self._agent_callback = None
        # Security knobs (mirror RESTAPIGateway defaults)
        self._api_key: str = ""
        self._rate_limit_per_minute: int = 60
        self._max_chat_bytes: int = 65536

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("web") or {}
        self._host = cfg.get("host", self._host)
        self._port = int(cfg.get("port", self._port))
        self._enabled = bool(cfg.get("enabled", True))
        # Auth: if api_key configured, require X-API-Key header on /api/* routes.
        # Reuse the same key as the REST API by default for operator convenience.
        self._api_key = cfg.get("api_key") or (
            (ctx.config.get("api") or {}).get("key") or ""
        )
        self._rate_limit_per_minute = int(cfg.get("rate_limit_per_minute", 60))
        self._max_chat_bytes = int(cfg.get("max_chat_bytes", 65536))
        if not self._enabled:
            return

    def bind_callback(self, cb) -> None:
        self._agent_callback = cb

    async def start(self) -> None:
        if not self._enabled:
            return
        try:
            import uvicorn  # type: ignore
            from fastapi import FastAPI, Request  # type: ignore
            from fastapi.responses import HTMLResponse, JSONResponse  # type: ignore
        except Exception:
            logger.warning("fastapi/uvicorn not installed — web UI disabled")
            return
        import hmac as _hmac
        app = FastAPI(title="One-Agent")

        # ---- Security middleware ----
        # Per-IP sliding-window rate limit (mirrors RESTAPIGateway logic)
        _rate_window: dict = {}  # ip -> list[timestamps]
        gw = self

        @app.middleware("http")
        async def security_middleware(request: Request, call_next):
            # 1) Body size limit for chat endpoints
            cl = request.headers.get("content-length")
            if cl and request.url.path.startswith("/api/chat"):
                try:
                    if int(cl) > gw._max_chat_bytes:
                        return JSONResponse(
                            {"error": "request body too large"}, status_code=413
                        )
                except ValueError:
                    pass
            # 2) Rate limit by client IP
            client_ip = request.client.host if request.client else "unknown"
            now = time.time()
            window = _rate_window.get(client_ip, [])
            window = [t for t in window if now - t < 60.0]
            if len(window) >= gw._rate_limit_per_minute:
                return JSONResponse(
                    {"error": "rate limit exceeded"}, status_code=429
                )
            window.append(now)
            _rate_window[client_ip] = window
            return await call_next(request)

        def _check_auth(request: Request) -> bool:
            """Return True if authorized (or no api_key configured)."""
            if not gw._api_key:
                return True
            provided = request.headers.get("X-API-Key", "")
            return bool(provided) and _hmac.compare_digest(provided, gw._api_key)

        ui_html = Path(__file__).with_name("index.html")
        if ui_html.exists():
            @app.get("/", response_class=HTMLResponse)
            async def root():
                return ui_html.read_text(encoding="utf-8")

        @app.post("/api/chat")
        async def api_chat(body: dict, request: Request):
            if not _check_auth(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            text = body.get("text", "")
            if not isinstance(text, str) or not text.strip():
                return JSONResponse({"error": "empty text"}, status_code=400)
            session_id = body.get("session_id", uuid.uuid4().hex[:12])
            if self._agent_callback is None:
                return {"reply": "[no agent callback bound]"}
            reply = await self._agent_callback(text, source="web", session_id=session_id)
            return {"reply": reply, "session_id": session_id}

        @app.post("/api/chat/stream")
        async def api_chat_stream(body: dict, request: Request):
            if not _check_auth(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            text = body.get("text", "")
            if not isinstance(text, str) or not text.strip():
                return JSONResponse({"error": "empty text"}, status_code=400)
            session_id = body.get("session_id", uuid.uuid4().hex[:12])
            # Clamp / sanitize caller-controlled params to prevent cost abuse
            model = body.get("model")
            temperature = body.get("temperature")
            if temperature is not None:
                try:
                    temperature = max(0.0, min(2.0, float(temperature)))
                except (TypeError, ValueError):
                    temperature = None
            max_tokens = body.get("max_tokens")
            if max_tokens is not None:
                try:
                    max_tokens = max(1, min(8192, int(max_tokens)))
                except (TypeError, ValueError):
                    max_tokens = None
            # Force tools=None: web stream must not execute arbitrary tools
            tools = None

            _llm = self.ctx.get_plugin("llm") if self.ctx else None
            if _llm is None:
                return {"error": "LLM provider not available"}

            from fastapi.responses import StreamingResponse
            import json as _json

            async def event_generator():
                msgs = [{"role": "user", "content": text}]
                if body.get("system"):
                    msgs.insert(0, {"role": "system", "content": body["system"]})
                yield f"data: {_json.dumps({'status': 'thinking', 'session_id': session_id})}\n\n"
                async for chunk in _llm.chat_completion_stream(
                    messages=msgs,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                ):
                    yield f"data: {_json.dumps(chunk)}\n\n"
                yield f"data: {_json.dumps({'done': True, 'session_id': session_id})}\n\n"

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.get("/api/status")
        async def status():
            if self.ctx is None:
                return {"ok": False, "uptime": 0}
            return {"ok": True, "uptime": int(time.time() - self.ctx.started_at)}

        config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning")
        server = uvicorn.Server(config)
        self._task = asyncio.create_task(server.serve())
        logger.info("web ui on http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await super().stop()
