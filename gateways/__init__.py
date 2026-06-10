"""Chat gateways — CLI, Telegram, WeCom (企业微信), web UI.

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
from typing import Any, Dict, Optional

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
                print("\n(interrupted)")
                return
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
                # Use per-message key to avoid race when multiple messages
                # from the same chat arrive in the same poll batch.
                msg_key = f"{session_id}-{u.get('update_id')}"
                event = asyncio.Event()
                self._sessions[msg_key] = event
                # dispatch — we rely on something to call into the agent
                if self.bus is not None:
                    self.bus.publish({
                        "type": "external_message",
                        "source": "telegram",
                        "session_id": msg_key,
                        "text": text,
                        "chat_id": chat_id,
                    })
                try:
                    await asyncio.wait_for(event.wait(), 120)
                    await self._send(chat_id, self._replies.get(msg_key, "[no reply]"))
                except asyncio.TimeoutError:
                    try:
                        await self._send(chat_id, "[timeout]")
                    except Exception:
                        pass
                except Exception:
                    logger.exception("telegram send error for chat %s", chat_id)
                finally:
                    # always clean up to prevent memory leak regardless of outcome
                    self._sessions.pop(msg_key, None)
                    self._replies.pop(msg_key, None)

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


# ------------- WeCom (企业微信) -------------------------------------------
class WeComGateway(Plugin):
    """企业微信机器人网关，支持两种模式：

    1. Webhook 模式（群机器人）：只需配置 webhook_key，向群聊推送消息。
       无法接收用户消息，仅用于主动推送通知。

    2. 应用消息模式（自建应用）：配置 corp_id + agent_id + secret，
       通过回调 URL 接收用户消息并回复。需要企业微信管理员权限。
    """

    name = "gateway_wecom"

    def __init__(self) -> None:
        super().__init__()
        self._mode: str = "webhook"  # webhook | app
        # webhook 模式
        self._webhook_key: str = ""
        # app 模式
        self._corp_id: str = ""
        self._agent_id: str = ""
        self._secret: str = ""
        self._token: str = ""           # 回调验证 token
        self._encoding_aes_key: str = ""  # 回调消息加密 key
        self._access_token: str = ""
        self._token_expires_at: float = 0
        # common
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._sessions: Dict[str, asyncio.Event] = {}
        self._replies: Dict[str, str] = {}
        self._callback_host: str = "0.0.0.0"
        self._callback_port: int = 18794

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("wecom") or {}
        if not cfg.get("enabled", False):
            logger.info("wecom disabled")
            return

        self._mode = cfg.get("mode", "webhook")
        self._webhook_key = cfg.get("webhook_key") or ""
        self._corp_id = cfg.get("corp_id") or ""
        self._agent_id = str(cfg.get("agent_id", ""))
        self._secret = cfg.get("secret") or ""
        self._token = cfg.get("callback_token") or ""
        self._encoding_aes_key = cfg.get("encoding_aes_key") or ""
        self._callback_host = cfg.get("callback_host", self._callback_host)
        self._callback_port = int(cfg.get("callback_port", self._callback_port))

        self._client = httpx.AsyncClient(timeout=30)

        if self._mode == "webhook":
            if not self._webhook_key:
                logger.warning("wecom webhook mode requires webhook_key")
                return
            logger.info("wecom webhook mode enabled")
        elif self._mode == "app":
            if not self._corp_id or not self._secret:
                logger.warning("wecom app mode requires corp_id and secret")
                return
            self.bus.subscribe("turn_completed", self._on_done)
            self._task = asyncio.create_task(self._start_callback_server())
            logger.info("wecom app mode enabled, callback on %s:%d",
                        self._callback_host, self._callback_port)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.aclose()
        await super().stop()

    # ------------------------------------------------ webhook 模式（仅推送）
    async def send_webhook(self, text: str, mentioned_list: Optional[list] = None) -> Dict:
        """通过群机器人 Webhook 推送消息到企业微信群聊。

        Args:
            text: 消息内容
            mentioned_list: @指定成员的 userid 列表，["all"] 则 @所有人
        """
        if not self._client or not self._webhook_key:
            return {"ok": False, "error": "webhook not configured"}
        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={self._webhook_key}"
        payload: Dict[str, Any] = {
            "msgtype": "text",
            "text": {"content": text[:4000]},
        }
        if mentioned_list:
            payload["text"]["mentioned_list"] = mentioned_list
        try:
            resp = await self._client.post(url, json=payload)
            data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.warning("wecom webhook error: %s", data)
                return {"ok": False, "error": data.get("errmsg", "unknown")}
            return {"ok": True}
        except Exception as exc:
            logger.warning("wecom webhook send failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def send_markdown(self, content: str) -> Dict:
        """通过 Webhook 推送 Markdown 格式消息。"""
        if not self._client or not self._webhook_key:
            return {"ok": False, "error": "webhook not configured"}
        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={self._webhook_key}"
        try:
            resp = await self._client.post(url, json={
                "msgtype": "markdown",
                "markdown": {"content": content[:8000]},
            })
            data = resp.json()
            return {"ok": data.get("errcode", 0) == 0}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------ app 模式（收发消息）
    async def _get_access_token(self) -> str:
        """获取企业微信 access_token，带缓存。"""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        try:
            resp = await self._client.get(url, params={  # type: ignore[union-attr]
                "corpid": self._corp_id,
                "corpsecret": self._secret,
            })
            data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.error("wecom gettoken failed: %s", data)
                return ""
            self._access_token = data["access_token"]
            self._token_expires_at = time.time() + data.get("expires_in", 7200) - 300
            return self._access_token
        except Exception as exc:
            logger.error("wecom gettoken error: %s", exc)
            return ""

    async def _send_app_message(self, user_id: str, text: str) -> bool:
        """通过应用消息接口回复用户。"""
        token = await self._get_access_token()
        if not token or not self._client:
            return False
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        try:
            resp = await self._client.post(url, json={
                "touser": user_id,
                "msgtype": "text",
                "agentid": int(self._agent_id),
                "text": {"content": text[:2048]},
            })
            data = resp.json()
            return data.get("errcode", 0) == 0
        except Exception as exc:
            logger.warning("wecom app send failed: %s", exc)
            return False

    async def _start_callback_server(self) -> None:
        """启动回调 HTTP 服务器接收企业微信消息推送。"""
        try:
            from fastapi import FastAPI, Request, Response
            import uvicorn
        except ImportError:
            logger.warning("fastapi not installed — wecom callback disabled")
            return

        app = FastAPI(title="Athena WeCom Callback")
        gateway = self  # closure reference

        @app.get("/wecom/callback")
        async def verify_url(request: Request):
            """企业微信验证 URL 有效性（GET 请求）。"""
            msg_signature = request.query_params.get("msg_signature", "")
            timestamp = request.query_params.get("timestamp", "")
            nonce = request.query_params.get("nonce", "")
            echostr = request.query_params.get("echostr", "")
            # 简化验证：直接返回 echostr（未加密模式）
            # 生产环境应使用 encoding_aes_key 解密验证
            if gateway._encoding_aes_key:
                try:
                    echostr = gateway._decrypt_message(echostr, msg_signature, timestamp, nonce)
                except Exception:
                    pass
            return Response(content=echostr, media_type="text/plain")

        @app.post("/wecom/callback")
        async def receive_message(request: Request):
            """接收企业微信消息推送（POST 请求）。"""
            body = await request.body()
            try:
                data = json.loads(body)
            except Exception:
                return Response(content="ok")

            xml_content = data.get("Content", "")
            # 简化处理：提取文本消息
            import re
            content_match = re.search(r"<Content><!\[CDATA\[(.*?)\]\]></Content>", xml_content)
            from_user_match = re.search(r"<FromUserName><!\[CDATA\[(.*?)\]\]></FromUserName>", xml_content)

            if not content_match or not from_user_match:
                return Response(content="ok")

            text = content_match.group(1).strip()
            user_id = from_user_match.group(1)

            if not text:
                return Response(content="ok")

            msg_key = f"wecom-{user_id}-{time.time_ns()}"
            event = asyncio.Event()
            gateway._sessions[msg_key] = event

            if gateway.bus is not None:
                gateway.bus.publish({
                    "type": "external_message",
                    "source": "wecom",
                    "session_id": msg_key,
                    "text": text,
                    "chat_id": user_id,
                })

            try:
                await asyncio.wait_for(event.wait(), 120)
                reply = gateway._replies.get(msg_key, "[no reply]")
                await gateway._send_app_message(user_id, reply)
            except asyncio.TimeoutError:
                await gateway._send_app_message(user_id, "[timeout]")
            finally:
                gateway._sessions.pop(msg_key, None)
                gateway._replies.pop(msg_key, None)

            return Response(content="ok")

        config = uvicorn.Config(app, host=self._callback_host, port=self._callback_port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    def _decrypt_message(self, echostr: str, signature: str, timestamp: str, nonce: str) -> str:
        """解密企业微信消息（需 encoding_aes_key）。"""
        import hashlib
        import base64
        aes_key = base64.b64decode(self._encoding_aes_key + "=")
        # Sort and hash for signature verification
        sort_list = sorted([self._token, timestamp, nonce, echostr])
        sha1 = hashlib.sha1("".join(sort_list).encode()).hexdigest()
        if sha1 != signature:
            raise ValueError("signature mismatch")
        # AES decrypt
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16]))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(base64.b64decode(echostr)) + decryptor.finalize()
        # Remove PKCS#7 padding
        pad_len = decrypted[-1]
        decrypted = decrypted[:-pad_len]
        # Remove 16-byte random prefix + 4-byte msg_len
        msg_len = int.from_bytes(decrypted[16:20], "big")
        return decrypted[20:20 + msg_len].decode("utf-8")

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
        await super().stop()
