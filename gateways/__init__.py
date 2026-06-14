"""Chat gateways — CLI, Telegram, WeCom, DingTalk, Feishu, Discord, Slack, web UI.

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
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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

        app = FastAPI(title="One-Agent WeCom Callback")
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


# ------------- DingTalk (钉钉) -------------------------------------------
class DingTalkGateway(Plugin):
    """钉钉机器人网关，支持两种模式：

    1. Webhook 模式（群机器人）：配置 webhook_url + secret，向群聊推送消息。
    2. Stream 模式（企业内部应用）：通过 Stream 长连接接收用户消息并回复。
    """

    name = "gateway_dingtalk"

    def __init__(self) -> None:
        super().__init__()
        self._mode: str = "webhook"
        # webhook 模式
        self._webhook_url: str = ""
        self._secret: str = ""
        # stream 模式
        self._client_id: str = ""
        self._client_secret: str = ""
        self._access_token: str = ""
        self._token_expires_at: float = 0
        # common
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._sessions: Dict[str, asyncio.Event] = {}
        self._replies: Dict[str, str] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("dingtalk") or {}
        if not cfg.get("enabled", False):
            logger.info("dingtalk disabled")
            return

        self._mode = cfg.get("mode", "webhook")
        self._webhook_url = cfg.get("webhook_url") or ""
        self._secret = cfg.get("secret") or ""
        self._client_id = cfg.get("client_id") or ""
        self._client_secret = cfg.get("client_secret") or ""

        self._client = httpx.AsyncClient(timeout=30)

        if self._mode == "webhook":
            if not self._webhook_url:
                logger.warning("dingtalk webhook mode requires webhook_url")
                return
            logger.info("dingtalk webhook mode enabled")
        elif self._mode == "stream":
            if not self._client_id or not self._client_secret:
                logger.warning("dingtalk stream mode requires client_id and client_secret")
                return
            self.bus.subscribe("turn_completed", self._on_done)
            self._task = asyncio.create_task(self._stream_loop())
            logger.info("dingtalk stream mode enabled")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        await super().stop()

    # ------------------------------------------------ webhook 模式
    async def send_webhook(self, text: str, at_all: bool = False) -> Dict:
        """通过群机器人 Webhook 推送消息。"""
        if not self._client or not self._webhook_url:
            return {"ok": False, "error": "webhook not configured"}
        import hashlib
        import hmac
        import base64
        import urllib.parse
        headers = {"Content-Type": "application/json"}
        url = self._webhook_url
        # 签名（如果配置了 secret）
        if self._secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{self._secret}"
            hmac_code = hmac.new(
                self._secret.encode(), string_to_sign.encode(), hashlib.sha256
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"{url}&timestamp={timestamp}&sign={sign}"
        payload: Dict[str, Any] = {
            "msgtype": "text",
            "text": {"content": text[:4000]},
            "at": {"isAtAll": at_all},
        }
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
            data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.warning("dingtalk webhook error: %s", data)
                return {"ok": False, "error": data.get("errmsg", "unknown")}
            return {"ok": True}
        except Exception as exc:
            logger.warning("dingtalk webhook send failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def send_markdown(self, title: str, text: str) -> Dict:
        """通过 Webhook 推送 Markdown 消息。"""
        if not self._client or not self._webhook_url:
            return {"ok": False, "error": "webhook not configured"}
        try:
            resp = await self._client.post(self._webhook_url, json={
                "msgtype": "markdown",
                "markdown": {"title": title[:64], "text": text[:8000]},
            })
            data = resp.json()
            return {"ok": data.get("errcode", 0) == 0}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------ stream 模式
    async def _get_access_token(self) -> str:
        """获取钉钉 access_token。"""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        try:
            resp = await self._client.post(  # type: ignore[union-attr]
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": self._client_id, "appSecret": self._client_secret},
            )
            data = resp.json()
            self._access_token = data.get("accessToken", "")
            self._token_expires_at = time.time() + data.get("expireIn", 7200) - 300
            return self._access_token
        except Exception as exc:
            logger.error("dingtalk gettoken error: %s", exc)
            return ""

    async def _send_message(self, conversation_id: str, text: str) -> bool:
        """通过钉钉 API 回复消息。"""
        token = await self._get_access_token()
        if not token or not self._client:
            return False
        try:
            resp = await self._client.post(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                headers={"x-acs-dingtalk-access-token": token},
                json={"robotCode": self._client_id, "userIds": [conversation_id],
                      "msgKey": "sampleText", "msgParam": json.dumps({"content": text[:2048]})},
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("dingtalk send failed: %s", exc)
            return False

    async def _stream_loop(self) -> None:
        """Stream 模式长连接轮询。"""
        while True:
            token = await self._get_access_token()
            if not token:
                await asyncio.sleep(10)
                continue
            try:
                # 钉钉 Stream 协议：通过 HTTP 长轮询获取事件
                resp = await self._client.get(  # type: ignore[union-attr]
                    "https://api.dingtalk.com/v1.0/gateway/connections/open",
                    headers={"x-acs-dingtalk-access-token": token},
                    json={"clientId": self._client_id, "subscriptions": [{"type": "EVENT", "topic": "/v1.0/im/bot/messages/get"}]},
                    timeout=60,
                )
                data = resp.json()
                for event in data.get("events", []) or []:
                    msg = event.get("data", {}) or {}
                    text = msg.get("text", {}).get("content", "").strip()
                    sender = msg.get("senderId", "")
                    conversation_id = msg.get("conversationId", "")
                    if not text:
                        continue
                    msg_key = f"dt-{conversation_id}-{time.time_ns()}"
                    evt = asyncio.Event()
                    self._sessions[msg_key] = evt
                    if self.bus is not None:
                        self.bus.publish({
                            "type": "external_message",
                            "source": "dingtalk",
                            "session_id": msg_key,
                            "text": text,
                            "chat_id": sender,
                        })
                    try:
                        await asyncio.wait_for(evt.wait(), 120)
                        reply = self._replies.get(msg_key, "[no reply]")
                        await self._send_message(sender, reply)
                    except asyncio.TimeoutError:
                        await self._send_message(sender, "[timeout]")
                    finally:
                        self._sessions.pop(msg_key, None)
                        self._replies.pop(msg_key, None)
            except Exception as exc:
                logger.warning("dingtalk stream error: %s", exc)
                await asyncio.sleep(5)

    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None:
            return
        sid = turn.session_id
        if sid in self._sessions:
            self._replies[sid] = turn.result or f"[error: {turn.error}]"
            self._sessions[sid].set()


# ------------- Feishu / Lark (飞书) ---------------------------------------
class FeishuGateway(Plugin):
    """飞书机器人网关，支持两种模式：

    1. Webhook 模式（群机器人）：配置 webhook_url + secret，向群聊推送消息。
    2. 事件订阅模式（自建应用）：通过回调 URL 接收消息并回复。
    """

    name = "gateway_feishu"

    def __init__(self) -> None:
        super().__init__()
        self._mode: str = "webhook"
        # webhook 模式
        self._webhook_url: str = ""
        self._secret: str = ""
        # app 模式
        self._app_id: str = ""
        self._app_secret: str = ""
        self._verification_token: str = ""
        self._encrypt_key: str = ""
        self._tenant_access_token: str = ""
        self._token_expires_at: float = 0
        # common
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._sessions: Dict[str, asyncio.Event] = {}
        self._replies: Dict[str, str] = {}
        self._callback_host: str = "0.0.0.0"
        self._callback_port: int = 18795

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("feishu") or {}
        if not cfg.get("enabled", False):
            logger.info("feishu disabled")
            return

        self._mode = cfg.get("mode", "webhook")
        self._webhook_url = cfg.get("webhook_url") or ""
        self._secret = cfg.get("secret") or ""
        self._app_id = cfg.get("app_id") or ""
        self._app_secret = cfg.get("app_secret") or ""
        self._verification_token = cfg.get("verification_token") or ""
        self._encrypt_key = cfg.get("encrypt_key") or ""
        self._callback_host = cfg.get("callback_host", self._callback_host)
        self._callback_port = int(cfg.get("callback_port", self._callback_port))

        self._client = httpx.AsyncClient(timeout=30)

        if self._mode == "webhook":
            if not self._webhook_url:
                logger.warning("feishu webhook mode requires webhook_url")
                return
            logger.info("feishu webhook mode enabled")
        elif self._mode == "app":
            if not self._app_id or not self._app_secret:
                logger.warning("feishu app mode requires app_id and app_secret")
                return
            self.bus.subscribe("turn_completed", self._on_done)
            self._task = asyncio.create_task(self._start_callback_server())
            logger.info("feishu app mode enabled, callback on %s:%d",
                        self._callback_host, self._callback_port)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        await super().stop()

    # ------------------------------------------------ webhook 模式
    async def send_webhook(self, text: str) -> Dict:
        """通过群机器人 Webhook 推送文本消息。"""
        if not self._client or not self._webhook_url:
            return {"ok": False, "error": "webhook not configured"}
        payload: Dict[str, Any] = {"msg_type": "text", "content": {"text": text[:4000]}}
        # 签名
        if self._secret:
            import hashlib
            import hmac
            import base64
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{self._secret}"
            sig = base64.b64encode(
                hmac.new(self._secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
            ).decode()
            payload["timestamp"] = timestamp
            payload["sign"] = sig
        try:
            resp = await self._client.post(self._webhook_url, json=payload)
            data = resp.json()
            if data.get("code", 0) != 0:
                return {"ok": False, "error": data.get("msg", "unknown")}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def send_rich_text(self, title: str, content: str) -> Dict:
        """通过 Webhook 推送富文本消息。"""
        if not self._client or not self._webhook_url:
            return {"ok": False, "error": "webhook not configured"}
        payload = {
            "msg_type": "post",
            "content": {"post": {"zh_cn": {"title": title[:64], "content": [[{"tag": "text", "text": content[:8000]}]]}}},
        }
        try:
            resp = await self._client.post(self._webhook_url, json=payload)
            data = resp.json()
            return {"ok": data.get("code", 0) == 0}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------ app 模式
    async def _get_tenant_token(self) -> str:
        if self._tenant_access_token and time.time() < self._token_expires_at:
            return self._tenant_access_token
        try:
            resp = await self._client.post(  # type: ignore[union-attr]
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            data = resp.json()
            self._tenant_access_token = data.get("tenant_access_token", "")
            self._token_expires_at = time.time() + data.get("expire", 7200) - 300
            return self._tenant_access_token
        except Exception as exc:
            logger.error("feishu gettoken error: %s", exc)
            return ""

    async def _reply_message(self, message_id: str, text: str) -> bool:
        token = await self._get_tenant_token()
        if not token or not self._client:
            return False
        try:
            resp = await self._client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"receive_id": message_id, "msg_type": "text",
                      "content": json.dumps({"text": text[:4000]})},
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("feishu reply failed: %s", exc)
            return False

    async def _start_callback_server(self) -> None:
        try:
            from fastapi import FastAPI, Request
            import uvicorn
        except ImportError:
            logger.warning("fastapi not installed — feishu callback disabled")
            return

        app = FastAPI(title="One-Agent Feishu Callback")
        gateway = self

        @app.post("/feishu/callback")
        async def callback(request: Request):
            body = await request.json()
            # URL 验证挑战
            if body.get("type") == "url_verification":
                return {"challenge": body.get("challenge", "")}
            # 事件回调
            event = body.get("event", {}) or {}
            msg = event.get("message", {}) or {}
            if msg.get("msg_type") != "text":
                return {"ok": True}
            content_str = msg.get("content", "{}")
            try:
                content = json.loads(content_str) if isinstance(content_str, str) else content_str
            except Exception:
                content = {}
            text = content.get("text", "").strip()
            chat_id = msg.get("chat_id", "")
            message_id = msg.get("message_id", "")
            if not text:
                return {"ok": True}

            msg_key = f"fs-{chat_id}-{time.time_ns()}"
            evt = asyncio.Event()
            gateway._sessions[msg_key] = evt

            if gateway.bus is not None:
                gateway.bus.publish({
                    "type": "external_message",
                    "source": "feishu",
                    "session_id": msg_key,
                    "text": text,
                    "chat_id": chat_id,
                })

            try:
                await asyncio.wait_for(evt.wait(), 120)
                reply = gateway._replies.get(msg_key, "[no reply]")
                await gateway._reply_message(message_id, reply)
            except asyncio.TimeoutError:
                await gateway._reply_message(message_id, "[timeout]")
            finally:
                gateway._sessions.pop(msg_key, None)
                gateway._replies.pop(msg_key, None)

            return {"ok": True}

        config = uvicorn.Config(app, host=self._callback_host, port=self._callback_port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None:
            return
        sid = turn.session_id
        if sid in self._sessions:
            self._replies[sid] = turn.result or f"[error: {turn.error}]"
            self._sessions[sid].set()


# ------------- Discord -----------------------------------------------------
class DiscordGateway(Plugin):
    """Discord Bot 网关，通过 Gateway WebSocket 长连接接收消息。

    使用 Discord Bot Token + httpx 轮询方式实现，无需 discord.py 依赖。
    """

    name = "gateway_discord"

    def __init__(self) -> None:
        super().__init__()
        self._token: str = ""
        self._allowed_channels: list = []
        self._base = "https://discord.com/api/v10"
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._sessions: Dict[str, asyncio.Event] = {}
        self._replies: Dict[str, str] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("discord") or {}
        self._token = cfg.get("bot_token") or ""
        self._allowed_channels = [str(c) for c in (cfg.get("allowed_channels") or [])]
        if not self._token or not cfg.get("enabled", False):
            logger.info("discord disabled")
            return
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={"Authorization": f"Bot {self._token}", "Content-Type": "application/json"},
        )
        self.bus.subscribe("turn_completed", self._on_done)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("discord gateway enabled")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        await super().stop()

    async def _poll_loop(self) -> None:
        """通过 Discord REST API 轮询消息（简化实现，无需 WebSocket）。"""
        # 获取 @me 的信息
        try:
            resp = await self._client.get(f"{self._base}/users/@me")  # type: ignore[union-attr]
            bot_data = resp.json()
            bot_id = bot_data.get("id", "")
        except Exception:
            logger.error("discord: failed to get bot identity")
            return

        # 获取已加入的频道列表
        last_message_ids: Dict[str, str] = {}
        while True:
            try:
                # 获取 Bot 所在的 Guilds
                resp = await self._client.get(f"{self._base}/users/@me/guilds")  # type: ignore[union-attr]
                guilds = resp.json() or []
                for guild in guilds:
                    guild_id = guild.get("id", "")
                    # 获取频道列表
                    ch_resp = await self._client.get(  # type: ignore[union-attr]
                        f"{self._base}/guilds/{guild_id}/channels"
                    )
                    channels = ch_resp.json() or []
                    for ch in channels:
                        ch_id = ch.get("id", "")
                        ch_type = ch.get("type", 0)
                        # 只处理文本频道 (type=0)
                        if ch_type != 0:
                            continue
                        if self._allowed_channels and ch_id not in self._allowed_channels:
                            continue
                        # 获取最近消息
                        params = {"limit": 5}
                        if last_message_ids.get(ch_id):
                            params["after"] = last_message_ids[ch_id]
                        msg_resp = await self._client.get(  # type: ignore[union-attr]
                            f"{self._base}/channels/{ch_id}/messages", params=params
                        )
                        messages = list(reversed(msg_resp.json() or []))
                        for msg in messages:
                            author = msg.get("author", {}) or {}
                            if author.get("id") == bot_id:
                                continue  # 忽略自己发的消息
                            text = (msg.get("content") or "").strip()
                            if not text:
                                continue
                            last_message_ids[ch_id] = msg.get("id", "")
                            msg_key = f"dc-{ch_id}-{msg.get('id')}"
                            evt = asyncio.Event()
                            self._sessions[msg_key] = evt
                            if self.bus is not None:
                                self.bus.publish({
                                    "type": "external_message",
                                    "source": "discord",
                                    "session_id": msg_key,
                                    "text": text,
                                    "chat_id": ch_id,
                                })
                            try:
                                await asyncio.wait_for(evt.wait(), 120)
                                reply = self._replies.get(msg_key, "[no reply]")
                                await self._send_message(ch_id, reply)
                            except asyncio.TimeoutError:
                                await self._send_message(ch_id, "[timeout]")
                            finally:
                                self._sessions.pop(msg_key, None)
                                self._replies.pop(msg_key, None)
                        # 更新 last_message_id
                        if messages:
                            last_message_ids[ch_id] = messages[-1].get("id", last_message_ids.get(ch_id, ""))
            except Exception as exc:
                logger.warning("discord poll error: %s", exc)
            await asyncio.sleep(3)

    async def _send_message(self, channel_id: str, text: str) -> None:
        if not self._client:
            return
        try:
            await self._client.post(
                f"{self._base}/channels/{channel_id}/messages",
                json={"content": text[:2000]},
            )
        except Exception as exc:
            logger.warning("discord send failed: %s", exc)

    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None:
            return
        sid = turn.session_id
        if sid in self._sessions:
            self._replies[sid] = turn.result or f"[error: {turn.error}]"
            self._sessions[sid].set()


# ------------- Slack -------------------------------------------------------
class SlackGateway(Plugin):
    """Slack Bot 网关，通过 Socket Mode 长连接接收消息。

    使用 Slack Web API + httpx 实现，无需 slack-sdk 依赖。
    """

    name = "gateway_slack"

    def __init__(self) -> None:
        super().__init__()
        self._token: str = ""          # xoxb-...
        self._app_token: str = ""      # xapp-... (Socket Mode)
        self._allowed_channels: list = []
        self._base = "https://slack.com/api"
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._sessions: Dict[str, asyncio.Event] = {}
        self._replies: Dict[str, str] = {}
        self._bot_user_id: str = ""

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("slack") or {}
        self._token = cfg.get("bot_token") or ""
        self._app_token = cfg.get("app_token") or ""
        self._allowed_channels = [str(c) for c in (cfg.get("allowed_channels") or [])]
        if not self._token or not cfg.get("enabled", False):
            logger.info("slack disabled")
            return
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        # 获取 Bot User ID
        try:
            resp = await self._client.get(f"{self._base}/auth.test")
            data = resp.json()
            self._bot_user_id = data.get("user_id", "")
        except Exception:
            pass
        self.bus.subscribe("turn_completed", self._on_done)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("slack gateway enabled")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        await super().stop()

    async def _poll_loop(self) -> None:
        """通过 RTM-like 轮询获取消息（简化实现）。"""
        last_ts: Dict[str, str] = {}
        while True:
            try:
                # 获取频道列表
                resp = await self._client.get(f"{self._base}/conversations.list",  # type: ignore[union-attr]
                    params={"types": "public_channel,private_channel", "limit": 100})
                channels = (resp.json().get("channels") or [])
                for ch in channels:
                    ch_id = ch.get("id", "")
                    if self._allowed_channels and ch_id not in self._allowed_channels:
                        continue
                    if not ch.get("is_member", False):
                        continue
                    params: Dict[str, Any] = {"channel": ch_id, "limit": 5}
                    if last_ts.get(ch_id):
                        params["oldest"] = last_ts[ch_id]
                    msg_resp = await self._client.get(  # type: ignore[union-attr]
                        f"{self._base}/conversations.history", params=params
                    )
                    messages = list(reversed(msg_resp.json().get("messages") or []))
                    for msg in messages:
                        user = msg.get("user", "")
                        if user == self._bot_user_id:
                            continue
                        text = (msg.get("text") or "").strip()
                        ts = msg.get("ts", "")
                        if not text:
                            continue
                        last_ts[ch_id] = ts
                        msg_key = f"sl-{ch_id}-{ts}"
                        evt = asyncio.Event()
                        self._sessions[msg_key] = evt
                        if self.bus is not None:
                            self.bus.publish({
                                "type": "external_message",
                                "source": "slack",
                                "session_id": msg_key,
                                "text": text,
                                "chat_id": ch_id,
                            })
                        try:
                            await asyncio.wait_for(evt.wait(), 120)
                            reply = self._replies.get(msg_key, "[no reply]")
                            await self._send_message(ch_id, reply)
                        except asyncio.TimeoutError:
                            await self._send_message(ch_id, "[timeout]")
                        finally:
                            self._sessions.pop(msg_key, None)
                            self._replies.pop(msg_key, None)
                    if messages:
                        last_ts[ch_id] = messages[-1].get("ts", last_ts.get(ch_id, ""))
            except Exception as exc:
                logger.warning("slack poll error: %s", exc)
            await asyncio.sleep(3)

    async def _send_message(self, channel: str, text: str) -> None:
        if not self._client:
            return
        try:
            await self._client.post(
                f"{self._base}/chat.postMessage",
                json={"channel": channel, "text": text[:4000]},
            )
        except Exception as exc:
            logger.warning("slack send failed: %s", exc)

    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None:
            return
        sid = turn.session_id
        if sid in self._sessions:
            self._replies[sid] = turn.result or f"[error: {turn.error}]"
            self._sessions[sid].set()


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
        except Exception:
            logger.warning("fastapi/uvicorn not installed — web UI disabled")
            return
        app = FastAPI(title="One-Agent")
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
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await super().stop()
