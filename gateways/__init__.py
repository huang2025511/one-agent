"""Chat gateways — CLI, Telegram, Discord, Slack, web UI.

Each gateway is a plugin that publishes ``user_message`` events and prints
or sends back the reply when a ``turn_completed`` event is produced.

We deliberately keep these integrations "poor man's": no heavy SDKs, just
httpx / stdlib.  That keeps the agent installable without optional deps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# ------------- CLI --------------------------------------------------------
class CLIGateway(Plugin):
    """Line-based interactive terminal interface."""

    name = "gateway_cli"

    def __init__(self) -> None:
        super().__init__()
        self._prompt = "athena> "
        self._session_id = uuid.uuid4().hex[:12]
        self._reply_available: asyncio.Event | None = None
        self._last_reply: str = ""

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self._prompt = (ctx.config.get("gateways") or {}).get("cli", {}).get("prompt", self._prompt)
        self.bus.subscribe("turn_completed", self._on_done)

    async def run_loop(self, send_to_agent) -> None:
        """Run the interactive REPL.  ``send_to_agent(text)`` should be an
        async function that triggers the agent pipeline."""
        print("Athena agent — type 'exit' to quit, 'help' for commands.")
        while True:
            try:
                line = input(self._prompt)
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print()
                continue
            line = line.strip()
            if not line:
                continue
            if line in {"quit", "exit", "q"}:
                return
            if line in {"help", "?"}:
                print("commands: exit | status | clear")
                continue
            if line == "status":
                print(f"session {self._session_id} up {int(time.monotonic())}s")
                continue
            if line == "clear":
                print("\033c", end="")
                continue
            self._reply_available = asyncio.Event()
            self._last_reply = ""
            await send_to_agent(line, source="cli", session_id=self._session_id)
            try:
                await asyncio.wait_for(self._reply_available.wait(), timeout=120)
            except asyncio.TimeoutError:
                print("[no reply in time]")
                continue
            print(self._last_reply)

    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None or turn.session_id != self._session_id:
            return
        self._last_reply = turn.result or f"[error: {turn.error}]"
        if self._reply_available is not None:
            self._reply_available.set()


# ------------- Telegram ---------------------------------------------------
class TelegramGateway(Plugin):
    """Minimal long-poll Telegram bot.  Set TELEGRAM_BOT_TOKEN to use."""

    name = "gateway_telegram"

    def __init__(self) -> None:
        super().__init__()
        self._token: Optional[str] = None
        self._allowed_users = []
        self._base = "https://api.telegram.org"
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._sessions: Dict[str, asyncio.Event] = {}
        self._replies: Dict[str, str] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("telegram") or {}
        self._token = cfg.get("bot_token") or ""
        self._allowed_users = cfg.get("allowed_users") or []
        if not self._token or not cfg.get("enabled", False):
            logger.info("telegram disabled")
            return
        self._client = httpx.AsyncClient(timeout=30)
        self.bus.subscribe("turn_completed", self._on_done)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.aclose()
        await super().stop()

    async def _loop(self) -> None:
        if not self._client or not self._token:
            return
        offset = 0
        while True:
            try:
                r = await self._client.get(
                    f"{self._base}/bot{self._token}/getUpdates",
                    params={"offset": offset, "timeout": 20, "limit": 5},
                )
                data = r.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram poll error: %s", exc)
                await asyncio.sleep(5)
                continue
            for u in data.get("result", []) or []:
                offset = max(offset, u.get("update_id", 0) + 1)
                msg = u.get("message") or {}
                text = msg.get("text")
                user_id = str((msg.get("from") or {}).get("id", ""))
                chat_id = msg.get("chat", {}).get("id")
                if not text or chat_id is None:
                    continue
                if self._allowed_users and user_id not in self._allowed_users:
                    await self._send(chat_id, "Sorry, you're not in the allowed list.")
                    continue
                session_id = f"tg-{chat_id}"
                event = asyncio.Event()
                self._sessions[session_id] = event
                # dispatch — we rely on something to call into the agent
                self.ctx.bus.publish({  # type: ignore[attr-defined]
                    "type": "external_message",
                    "source": "telegram",
                    "session_id": session_id,
                    "text": text,
                    "chat_id": chat_id,
                })
                try:
                    await asyncio.wait_for(event.wait(), 120)
                    await self._send(chat_id, self._replies.get(session_id, "[no reply]"))
                except asyncio.TimeoutError:
                    await self._send(chat_id, "[timeout]")

    async def _send(self, chat_id: int, text: str) -> None:
        if not self._client or not self._token:
            return
        await self._client.post(
            f"{self._base}/bot{self._token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
        )

    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None:
            return
        session_id = turn.session_id
        if session_id in self._sessions:
            self._replies[session_id] = turn.result or f"[error: {turn.error}]"
            self._sessions[session_id].set()


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

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("web") or {}
        self._host = cfg.get("host", self._host)
        self._port = int(cfg.get("port", self._port))
        self._enabled = bool(cfg.get("enabled", True))
        if not self._enabled:
            return

    def bind_callback(self, cb) -> None:
        self._agent_callback = cb

    async def start(self) -> None:
        if not self._enabled:
            return
        try:
            import uvicorn  # type: ignore
            from fastapi import FastAPI  # type: ignore
            from fastapi.responses import HTMLResponse  # type: ignore
            from fastapi.staticfiles import StaticFiles  # type: ignore
        except Exception:
            logger.warning("fastapi/uvicorn not installed — web UI disabled")
            return
        app = FastAPI(title="Athena")
        ui_html = Path(__file__).with_name("index.html")
        if ui_html.exists():
            @app.get("/", response_class=HTMLResponse)
            async def root():
                return ui_html.read_text(encoding="utf-8")

        @app.post("/api/chat")
        async def api_chat(body: dict):
            text = body.get("text", "")
            session_id = body.get("session_id", uuid.uuid4().hex[:12])
            if self._agent_callback is None:
                return {"reply": "[no agent callback bound]"}
            reply = await self._agent_callback(text, source="web", session_id=session_id)
            return {"reply": reply, "session_id": session_id}

        @app.get("/api/status")
        async def status():
            return {"ok": True, "uptime": int(time.time() - (self.ctx.started_at if self.ctx else time.time()))}

        config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning")
        server = uvicorn.Server(config)
        self._task = asyncio.create_task(server.serve())
        logger.info("web ui on http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await super().stop()
