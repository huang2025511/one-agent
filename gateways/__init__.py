"""Chat gateways — CLI, web UI, and messaging re-exports.

CLIGateway and WebGateway live here directly.  Messaging gateways
(Telegram, WeCom, DingTalk, Feishu, Discord, Slack) are imported lazily
via ``__getattr__`` so that ``import gateways`` does not pull in httpx
and all six messaging modules unless a gateway is actually requested.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from core.plugin import Plugin

# 修复 ForwardRef 隐患（同 api/__init__.py 的 bug）：
# 文件有 `from __future__ import annotations`，所有类型注解会变成字符串
# ForwardRef。FastAPI 解析 `request: Request` 时需要在模块全局命名空间
# 找到 `Request`。但 Request 之前只在 start() 方法内局部导入，导致
# FastAPI 解析 ForwardRef('Request') 失败 → WebGateway 的 /api/chat、
# /api/chat/stream 路由 422。在顶层导入让 ForwardRef 能解析。
# fastapi 是可选依赖，用 try/except 保护导入。
try:
    from fastapi import Request as _Request  # noqa: F401
    Request = _Request  # 让模块全局命名空间可见
except ImportError:  # pragma: no cover — fastapi 未装时的降级路径
    Request = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Lazy gateway class loader — only imports a messaging gateway module when
# that specific class is requested via ``getattr(gateways, ClassName)``.
# This keeps ``import gateways`` cheap for CLI-only runs.
_GATEWAY_CLASS_MAP = {
    "TelegramGateway": "gateways.messaging",
    "WeComGateway": "gateways.messaging",
    "DingTalkGateway": "gateways.messaging",
    "FeishuGateway": "gateways.messaging",
    "DiscordGateway": "gateways.messaging",
    "SlackGateway": "gateways.messaging",
    "WeChatPersonalGateway": "gateways.wechat_personal",
}


def __getattr__(name: str):
    """Lazily import gateway classes on first attribute access."""
    if name in _GATEWAY_CLASS_MAP:
        import importlib
        module = importlib.import_module(_GATEWAY_CLASS_MAP[name])
        cls = getattr(module, name)
        globals()[name] = cls  # cache for subsequent access
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None or turn.session_id != self._session_id:
            return
        self._last_reply = turn.result if turn.result is not None else f"[error: {turn.error}]"
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
        # Per-IP sliding-window rate limit (mirrors RESTAPIGateway logic).
        # Uses a dict with periodic cleanup of stale IPs to prevent unbounded
        # memory growth from spoofed/varied client IPs.
        _rate_window: dict = {}  # ip -> list[timestamps]
        _rate_cleanup_counter = 0  # cleanup every N requests
        _RATE_CLEANUP_INTERVAL = 200
        gw = self

        @app.middleware("http")
        async def security_middleware(request: Request, call_next):
            nonlocal _rate_cleanup_counter
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
            # 3) Periodic cleanup of stale IPs to prevent memory leak
            _rate_cleanup_counter += 1
            if _rate_cleanup_counter >= _RATE_CLEANUP_INTERVAL:
                _rate_cleanup_counter = 0
                stale_ips = [
                    ip for ip, ts in _rate_window.items()
                    if not ts or now - ts[-1] > 120.0
                ]
                for ip in stale_ips:
                    del _rate_window[ip]
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

            import json as _json

            from fastapi.responses import StreamingResponse

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
