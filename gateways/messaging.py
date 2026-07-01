"""Messaging gateways — Telegram, WeCom, DingTalk, Feishu, Discord, Slack.

Each gateway is a plugin that publishes user_message events and sends back
replies via the respective messaging platform's API.

All integrations use httpx / stdlib — no heavy SDK dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


class BaseMessagingGateway(Plugin):
    """Base class for all messaging gateways with common message handling logic."""

    def __init__(self) -> None:
        super().__init__()
        self._sessions: Dict[str, asyncio.Event] = {}
        self._replies: Dict[str, str] = {}
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def _on_done(self, event) -> None:
        """Common message completion handler."""
        turn = event.get("turn")
        if turn is None:
            return
        sid = turn.session_id
        if sid in self._sessions:
            self._replies[sid] = turn.result if turn.result is not None else f"[error: {turn.error}]"
            self._sessions[sid].set()

    async def _wait_and_reply(self, msg_key: str, chat_id, send_fn, timeout: float = 120) -> None:
        """Wait for a turn to complete, then send the reply.

        Runs as a background task so the gateway's poll loop is not blocked
        while waiting — previously every gateway awaited this inline,
        serializing all message processing for that platform.
        """
        event = self._sessions.get(msg_key)
        if event is None:
            return
        try:
            await asyncio.wait_for(event.wait(), timeout)
            reply = self._replies.get(msg_key, "[no reply]")
        except asyncio.TimeoutError:
            reply = "[timeout]"
        except Exception:
            logger.exception("gateway wait_and_reply error for %s", msg_key)
            reply = "[error]"
        try:
            await send_fn(chat_id, reply)
        except Exception:
            logger.exception("gateway send error for %s", chat_id)
        finally:
            self._sessions.pop(msg_key, None)
            self._replies.pop(msg_key, None)

    def _spawn(self, coro, **kwargs) -> asyncio.Task:
        """Spawn a background task with error logging on failure."""
        task = asyncio.create_task(coro(**kwargs))
        msg_key = kwargs.get("msg_key", "")
        task.add_done_callback(
            lambda t: logger.exception("gateway background task failed for %s", msg_key)
            if t.exception() else None
        )
        return task

    async def stop(self) -> None:
        """Common cleanup: cancel the polling task and close the HTTP client."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        await super().stop()


# ------------- Telegram ---------------------------------------------------
class TelegramGateway(BaseMessagingGateway):
    """Minimal long-poll Telegram bot.  Set TELEGRAM_BOT_TOKEN to use."""

    name = "gateway_telegram"

    def __init__(self) -> None:
        super().__init__()
        self._token: Optional[str] = None
        self._allowed_users = []
        self._base = "https://api.telegram.org"

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
                # Spawn a background task to wait for the reply and send it,
                # so the poll loop can continue processing other messages.
                self._spawn(
                    self._wait_and_reply,
                    msg_key=msg_key,
                    chat_id=chat_id,
                    send_fn=self._send,
                )

    async def _send(self, chat_id: int, text: str) -> None:
        if not self._client or not self._token:
            return
        await self._client.post(
            f"{self._base}/bot{self._token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
        )


# ------------- WeCom (企业微信) -------------------------------------------
class WeComGateway(BaseMessagingGateway):
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
            # Fail-closed: the callback signature verification in verify_url()
            # and receive_message() skips validation when _token is empty.
            # We MUST refuse to start the callback server without a token,
            # otherwise an attacker can inject forged messages freely.
            if not self._token:
                logger.warning("wecom app mode requires callback_token for signature verification")
                return
            self.bus.subscribe("turn_completed", self._on_done)
            self._task = asyncio.create_task(self._start_callback_server())
            logger.info("wecom app mode enabled, callback on %s:%d",
                        self._callback_host, self._callback_port)


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
            logger.exception("wecom gettoken error: %s", exc)
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
            import uvicorn
            from fastapi import FastAPI, Request, Response
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

            # Security: fail-closed signature verification.
            # If a token is configured, signature MUST be present and valid;
            # if no token is configured, callback server should not be started
            # (setup() refuses to start), so we still reject missing signatures.
            if gateway._token:
                if not msg_signature:
                    return Response(content="missing signature", status_code=403)
                import hashlib
                sort_list = sorted([gateway._token, timestamp, nonce, echostr])
                expected_sig = hashlib.sha1("".join(sort_list).encode()).hexdigest()
                if not hmac.compare_digest(expected_sig, msg_signature):
                    logger.warning("wecom callback signature verification failed")
                    return Response(content="signature verification failed", status_code=403)

            if gateway._encoding_aes_key:
                try:
                    echostr = gateway._decrypt_message(echostr, msg_signature, timestamp, nonce)
                except Exception:
                    logger.warning("wecom callback decryption failed")
                    return Response(content="decryption failed", status_code=403)
            return Response(content=echostr, media_type="text/plain")

        @app.post("/wecom/callback")
        async def receive_message(request: Request):
            """接收企业微信消息推送（POST 请求）。"""
            body = await request.body()

            # Security: fail-closed signature verification for POST messages
            msg_signature = request.query_params.get("msg_signature", "")
            timestamp = request.query_params.get("timestamp", "")
            nonce = request.query_params.get("nonce", "")
            if gateway._token:
                if not msg_signature:
                    return Response(content="missing signature", status_code=403)
                import hashlib
                sort_list = sorted([gateway._token, timestamp, nonce, body.decode("utf-8", errors="replace")])
                expected_sig = hashlib.sha1("".join(sort_list).encode()).hexdigest()
                if not hmac.compare_digest(expected_sig, msg_signature):
                    logger.warning("wecom POST callback signature verification failed")
                    return Response(content="signature verification failed", status_code=403)

            # WeCom POST body is XML, not JSON. Parse with ElementTree (safe against
            # XXE by default in Python's stdlib xml.etree).
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(body)
            except Exception:
                logger.warning("wecom POST callback: invalid XML body")
                return Response(content="ok")

            # If encoding_aes_key is configured, the body is encrypted; decrypt first
            if gateway._encoding_aes_key:
                try:
                    encrypted = root.findtext("Encrypt", default="")
                    decrypted_xml = gateway._decrypt_message(
                        encrypted, msg_signature, timestamp, nonce)
                    root = ET.fromstring(decrypted_xml.encode("utf-8"))
                except Exception:
                    logger.warning("wecom POST callback decryption failed")
                    return Response(content="decryption failed", status_code=403)

            text = (root.findtext("Content") or "").strip()
            user_id = root.findtext("FromUserName") or ""

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

            # Respond immediately so WeCom doesn't retry the callback.
            # The reply is sent asynchronously via a background task.
            asyncio.create_task(
                gateway._wait_and_reply(msg_key, user_id, gateway._send_app_message)
            )

            return Response(content="ok")

        config = uvicorn.Config(app, host=self._callback_host, port=self._callback_port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    def _decrypt_message(self, echostr: str, signature: str, timestamp: str, nonce: str) -> str:
        """解密企业微信消息（需 encoding_aes_key）。"""
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


# ------------- DingTalk (钉钉) -------------------------------------------
class DingTalkGateway(BaseMessagingGateway):
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


    # ------------------------------------------------ webhook 模式
    async def send_webhook(self, text: str, at_all: bool = False) -> Dict:
        """通过群机器人 Webhook 推送消息。"""
        if not self._client or not self._webhook_url:
            return {"ok": False, "error": "webhook not configured"}
        import base64
        import hmac
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
            logger.exception("dingtalk gettoken error: %s", exc)
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
        """Stream 模式：通过钉钉 Stream 协议（WebSocket）接收事件。

        钉钉 Stream 协议流程：
        1. POST https://api.dingtalk.com/v1.0/gateway/connections/open 获取
           WebSocket endpoint (wss://...) 和 ticket。
        2. 用 WebSocket 客户端连接 endpoint，发送注册帧 {"ticket": ...}。
        3. 在 WS 长连接上接收事件推送，回复 Pong 心跳。
        """
        # 尝试加载 WebSocket 客户端库（优先 websockets，其次 aiohttp）
        ws_connect = None
        _aiohttp_session = None  # track for cleanup
        try:
            import websockets  # type: ignore
            async def _ws_connect(uri):
                return await websockets.connect(uri)
            ws_connect = _ws_connect
        except ImportError:
            try:
                import aiohttp  # type: ignore
                async def _ws_connect(uri):
                    nonlocal _aiohttp_session
                    # Reuse a single ClientSession across reconnects to
                    # avoid leaking sessions (each ClientSession holds a
                    # connection pool and file descriptors).
                    if _aiohttp_session is None or _aiohttp_session.closed:
                        _aiohttp_session = aiohttp.ClientSession()
                    return await _aiohttp_session.ws_connect(uri)
                ws_connect = _ws_connect
            except ImportError:
                logger.warning(
                    "dingtalk stream mode requires 'websockets' or 'aiohttp' "
                    "package; neither is installed — stream mode disabled"
                )
                return

        while True:
            token = await self._get_access_token()
            if not token:
                await asyncio.sleep(10)
                continue
            try:
                # Step 1: open connection — must be POST (not GET)
                resp = await self._client.post(  # type: ignore[union-attr]
                    "https://api.dingtalk.com/v1.0/gateway/connections/open",
                    headers={"x-acs-dingtalk-access-token": token},
                    json={
                        "clientId": self._client_id,
                        "subscriptions": [
                            {"type": "EVENT", "topic": "/v1.0/im/bot/messages/get"}
                        ],
                    },
                    timeout=30,
                )
                conn = resp.json()
                endpoint = conn.get("endpoint")
                ticket = conn.get("ticket")
                if not endpoint or not ticket:
                    logger.warning("dingtalk stream: no endpoint/ticket in response: %s", conn)
                    await asyncio.sleep(10)
                    continue

                # Step 2: connect via WebSocket
                async with await ws_connect(endpoint) as ws:
                    # 注册帧
                    await ws.send(json.dumps({"ticket": ticket}))
                    # Step 3: receive loop
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=120)
                        except asyncio.TimeoutError:
                            # 心跳：发送 ping 保活
                            try:
                                await ws.send(json.dumps({"type": "ping"}))
                            except Exception:
                                break
                            continue
                        try:
                            frame = json.loads(raw)
                        except Exception:
                            continue
                        # 钉钉心跳协议：收到 SYSTEM 心跳需回复 ack
                        if frame.get("type") == "SYSTEM":
                            headers = frame.get("headers", {})
                            if headers.get("contentType") == "application/json; charset=UTF-8":
                                await ws.send(json.dumps({
                                    "code": 200,
                                    "headers": {"contentType": "application/json; charset=UTF-8"},
                                    "message": "OK",
                                    "data": json.dumps({"messageId": headers.get("messageId")}),
                                }))
                            continue
                        # 业务事件
                        data = frame.get("data") or {}
                        msg = data.get("content") or data
                        if isinstance(msg, str):
                            try:
                                msg = json.loads(msg)
                            except Exception:
                                msg = {}
                        text = (msg.get("text", {}).get("content") or "").strip()
                        sender = msg.get("senderId") or msg.get("senderStaffId") or ""
                        conversation_id = msg.get("conversationId") or ""
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
                        # Non-blocking: spawn background task to wait + reply
                        self._spawn(
                            self._wait_and_reply,
                            msg_key=msg_key,
                            chat_id=sender,
                            send_fn=self._send_message,
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("dingtalk stream error: %s", exc)
                await asyncio.sleep(5)


# ------------- Feishu / Lark (飞书) ---------------------------------------
class FeishuGateway(BaseMessagingGateway):
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
            # Fail-closed: the callback verification_token check in the event
            # handler skips validation when _verification_token is empty. We
            # MUST refuse to start without it, otherwise forged events pass.
            if not self._verification_token:
                logger.warning("feishu app mode requires verification_token for event verification")
                return
            self.bus.subscribe("turn_completed", self._on_done)
            self._task = asyncio.create_task(self._start_callback_server())
            logger.info("feishu app mode enabled, callback on %s:%d",
                        self._callback_host, self._callback_port)


    # ------------------------------------------------ webhook 模式
    async def send_webhook(self, text: str) -> Dict:
        """通过群机器人 Webhook 推送文本消息。"""
        if not self._client or not self._webhook_url:
            return {"ok": False, "error": "webhook not configured"}
        payload: Dict[str, Any] = {"msg_type": "text", "content": {"text": text[:4000]}}
        # 签名
        if self._secret:
            import base64
            import hashlib
            import hmac
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
            logger.exception("feishu gettoken error: %s", exc)
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
            import uvicorn
            from fastapi import FastAPI, Request
        except ImportError:
            logger.warning("fastapi not installed — feishu callback disabled")
            return

        app = FastAPI(title="One-Agent Feishu Callback")
        gateway = self

        @app.post("/feishu/callback")
        async def callback(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw)
            except Exception:
                return {"error": "invalid json"}, 400

            # Decrypt payload if encrypt_key is configured (Feishu AES-256-CBC)
            if gateway._encrypt_key and isinstance(body, dict) and body.get("encrypt"):
                try:
                    import base64 as _base64
                    import hashlib as _hashlib

                    from cryptography.hazmat.primitives.ciphers import (  # type: ignore
                        Cipher,
                        algorithms,
                        modes,
                    )
                    key = _hashlib.sha256(gateway._encrypt_key.encode()).digest()
                    encrypted_b64 = body["encrypt"]
                    encrypted = _base64.b64decode(encrypted_b64)
                    iv, ciphertext = encrypted[:16], encrypted[16:]
                    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                    decryptor = cipher.decryptor()
                    padded = decryptor.update(ciphertext) + decryptor.finalize()
                    pad_len = padded[-1]
                    plaintext = padded[:-pad_len].decode("utf-8")
                    body = json.loads(plaintext)
                except ImportError:
                    logger.warning("cryptography not installed — cannot decrypt Feishu payload")
                    return {"error": "decryption unavailable"}, 500
                except Exception:
                    logger.warning("feishu callback decryption failed")
                    return {"error": "decryption failed"}, 403

            # Security: verify verification_token (fail-closed).
            # Feishu includes the token in header.token for both url_verification
            # and event callbacks. Reject any request missing or mismatching it
            # when a token is configured.
            if gateway._verification_token:
                header = (body.get("header") or {}) if isinstance(body, dict) else {}
                token_in_req = header.get("token") or body.get("token") or ""
                if not token_in_req:
                    return {"error": "missing verification token"}, 403
                if not hmac.compare_digest(token_in_req, gateway._verification_token):
                    logger.warning("feishu callback token verification failed")
                    return {"error": "token verification failed"}, 403

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

            # Respond immediately; reply sent via background task
            asyncio.create_task(
                gateway._wait_and_reply(msg_key, message_id, gateway._reply_message)
            )

            return {"ok": True}

        config = uvicorn.Config(app, host=self._callback_host, port=self._callback_port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()


# ------------- Discord -----------------------------------------------------
class DiscordGateway(BaseMessagingGateway):
    """Discord Bot 网关，通过 Gateway WebSocket 长连接接收消息。

    使用 Discord Bot Token + httpx 轮询方式实现，无需 discord.py 依赖。
    """

    name = "gateway_discord"

    def __init__(self) -> None:
        super().__init__()
        self._token: str = ""
        self._allowed_channels: list = []
        self._base = "https://discord.com/api/v10"

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
                            # Non-blocking: spawn background task to wait + reply
                            self._spawn(
                                self._wait_and_reply,
                                msg_key=msg_key,
                                chat_id=ch_id,
                                send_fn=self._send_message,
                            )
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


# ------------- Slack -------------------------------------------------------
class SlackGateway(BaseMessagingGateway):
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
        except Exception as exc:
            logger.warning("slack: failed to get bot user_id (%s) — self-reply filtering disabled, this is dangerous", exc)
        if not self._bot_user_id:
            # Without a valid bot_user_id, we cannot filter out our own
            # messages, leading to an infinite self-reply loop. Refuse to
            # start the poll loop in this case.
            logger.error("slack: bot user_id is empty, refusing to start poll loop to prevent self-reply loop")
            await self._client.aclose()
            self._client = None
            return
        self.bus.subscribe("turn_completed", self._on_done)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("slack gateway enabled")


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
                        # Non-blocking: spawn background task to wait + reply
                        asyncio.create_task(
                            self._wait_and_reply(msg_key, ch_id, self._send_message)
                        )
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
