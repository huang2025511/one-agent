"""Personal WeChat (个人微信) gateway — using Tencent iLink Bot API.

This gateway logs into a **personal** WeChat account using the official iLink
Bot API (not web WeChat / not itchat).

Requirements
------------
``pip install aiohttp cryptography``

Usage
-----
1. Enable in config:

   .. code-block:: yaml

      gateways:
        wechat_personal:
          enabled: true
          allowed_users: []  # empty = allow all

2. Start one-agent
3. Type "微信登录" to get QR code URL
4. Open URL in browser, scan with WeChat
5. Agent listens for messages and answers
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.events import Event
from core.plugin import Plugin

logger = logging.getLogger(__name__)

AIOHTTP_AVAILABLE = False
CRYPTO_AVAILABLE = False

try:
    import aiohttp  # noqa: F401
    AIOHTTP_AVAILABLE = True
except ImportError:
    pass

try:
    from cryptography.hazmat.backends import default_backend  # noqa: F401
    CRYPTO_AVAILABLE = True
except ImportError:
    pass

ILINK_APP_ID = "wx1124bf4936a4d8cf"
ILINK_APP_CLIENT_VERSION = 100000223
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
EP_GET_QRCODE = "ilink/bot/getqrcode"
EP_CHECK_QRCODE = "ilink/bot/checkqrcode"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MSG = "ilink/bot/sendmsg"

MSG_TYPE_TEXT = 1

DATA_DIR = Path.home() / ".one-agent" / "weixin" / "accounts"


def _sanitize_chat_id(chat_id: str) -> str:
    return "".join(c if c.isalnum() or c in "@.-_" else "_" for c in chat_id)


def _account_path(account_id: str) -> Path:
    safe = _sanitize_chat_id(account_id)
    return DATA_DIR / f"{safe}.json"


def _sync_path(account_id: str) -> Path:
    safe = _sanitize_chat_id(account_id)
    return DATA_DIR / f"{safe}.sync.json"


def _load_sync_buf(account_id: str) -> str:
    path = _sync_path(account_id)
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""


def _save_sync_buf(account_id: str, sync_buf: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _sync_path(account_id).write_text(sync_buf, encoding="utf-8")
    except Exception:
        pass


def _save_credentials(account_id: str, token: str, base_url: str, user_id: str = "") -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "account_id": account_id,
            "token": token,
            "base_url": base_url,
            "user_id": user_id,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _account_path(account_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("wechat_personal: failed to save credentials: %s", exc)


async def _api_post(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: str = "",
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "Content-Type": "application/json",
    }
    if token:
        headers["iLink-Bot-Token"] = token

    async def _do():
        async with session.post(url, json=payload, headers=headers) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _api_get(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }

    async def _do():
        async with session.get(url, headers=headers) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _get_updates(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    try:
        return await _api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_msg(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    chat_id: str,
    content: str,
    msg_type: int = MSG_TYPE_TEXT,
) -> Dict[str, Any]:
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MSG,
        payload={
            "to_id": chat_id,
            "msg_type": msg_type,
            "content": content,
        },
        token=token,
        timeout_ms=10000,
    )


class WeChatPersonalGateway(Plugin):
    """个人微信网关 — 使用腾讯 iLink Bot API."""

    name = "gateway_wechat_personal"

    def __init__(self) -> None:
        super().__init__()
        self._account_id: str = ""
        self._token: str = ""
        self._base_url: str = ILINK_BASE_URL
        self._user_id: str = ""
        self._allowed_users: List[str] = []
        self._session: Optional["aiohttp.ClientSession"] = None
        self._running: bool = False
        self._poll_task: Optional[asyncio.Task] = None
        self._login_task: Optional[asyncio.Task] = None
        self._qr_url: str = ""
        self._msg_tasks: set = set()
        self._last_heartbeat: float = 0
        self._initial_sync_buf: str = ""
        # 实时进度反馈：追踪每个会话的进度状态
        self._progress_last_sent: Dict[str, float] = {}   # chat_id → 上次进度消息时间
        self._progress_count: Dict[str, int] = {}           # chat_id → 本次 turn 已发进度条数
        self._heartbeat_tasks: Dict[str, asyncio.Task] = {}  # chat_id → 心跳任务
        # 与全仓库其它 gateway/executor 约定一致（WebGateway / RESTAPIGateway /
        # SystemExecutor / DockerExecutor / BrowserExecutor 都用 _enabled 表示
        # 「配置层是否启用」。_running 表示「已连接并正在轮询」是另一层语义，
        # 不能替代 _enabled。setup() 会根据 config 把 enabled_cfg 赋到这里。
        self._enabled: bool = False

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("wechat_personal") or {}
        enabled_cfg = bool(cfg.get("enabled", False))
        # 修复 bug：之前 enabled_cfg 只用来打日志，没赋给 self._enabled，
        # 导致外部无法通过 self._enabled 查询网关是否启用（全仓库约定）。
        self._enabled = enabled_cfg
        self._allowed_users = [str(u).strip() for u in (cfg.get("allowed_users") or []) if str(u).strip()]

        saved = self._find_saved_account()
        logger.info("wechat_personal: auto-discovering saved accounts (account_id=%s, token=%s)",
                    bool(self._account_id), bool(self._token))

        if saved and saved.get("token"):
            self._account_id = saved.get("account_id", "")
            self._token = saved.get("token", "")
            self._base_url = saved.get("base_url", ILINK_BASE_URL)
            self._user_id = saved.get("user_id", "")
            logger.info("wechat_personal: auto-loaded saved account %s", self._account_id[:8])
            logger.info("wechat_personal enabled, account=%s", self._account_id[:8])
            await self._connect()
        elif enabled_cfg:
            logger.info("wechat_personal enabled but no saved credentials, waiting for QR login")
        else:
            logger.info("wechat_personal disabled, account=none")

        self.bus.subscribe("turn_completed", self._on_turn_completed)
        self.bus.subscribe("turn_progress", self._on_turn_progress)

    def _find_saved_account(self) -> Optional[Dict[str, Any]]:
        logger.info("wechat_personal: _find_saved_account, dir=%s, exists=%s", DATA_DIR, DATA_DIR.exists())
        if not DATA_DIR.exists():
            return None
        best: Optional[Dict[str, Any]] = None
        best_time = 0.0
        files_found = []
        for path in DATA_DIR.glob("*.json"):
            files_found.append(str(path))
            if path.name.endswith(".sync.json"):
                logger.info("wechat_personal: found sync file %s, skipping", path.name)
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                saved_at = data.get("saved_at", "")
                has_token = bool(data.get("token", ""))
                logger.info("wechat_personal: found account file %s, has_token=%s, saved_at=%s",
                           path.name, has_token, saved_at)
                if not data.get("token"):
                    continue
                ts = 0.0
                try:
                    ts = time.mktime(time.strptime(saved_at, "%Y-%m-%dT%H:%M:%SZ"))
                except Exception:
                    pass
                if ts > best_time:
                    best_time = ts
                    best = data
            except Exception as exc:
                logger.warning("wechat_personal: error reading %s: %s", path.name, exc)
                continue
        logger.info("wechat_personal: _find_saved_account done, files_found=%s, best=%s",
                   files_found, best is not None)
        return best

    async def stop(self) -> None:
        # 清理所有心跳任务
        for chat_id in list(self._heartbeat_tasks.keys()):
            self._stop_heartbeat(chat_id)
        await self._disconnect()
        await super().stop()

    async def login(self) -> bool:
        if self._running:
            return True
        if self._login_task is not None and not self._login_task.done():
            return True
        if not AIOHTTP_AVAILABLE or not CRYPTO_AVAILABLE:
            logger.error("wechat_personal: aiohttp and cryptography are required")
            return False

        saved = self._find_saved_account()
        if saved and saved.get("token"):
            logger.info("wechat_personal: trying saved credentials for %s", saved.get("account_id", "")[:8])
            self._account_id = saved.get("account_id", "")
            self._token = saved.get("token", "")
            self._base_url = saved.get("base_url", ILINK_BASE_URL)
            self._user_id = saved.get("user_id", "")
            await self._connect()
            if self._running:
                self._qr_url = ""
                logger.info("wechat_personal: reconnected with saved credentials")
                return True
            else:
                logger.warning("wechat_personal: saved credentials failed, falling back to QR login")

        self._login_task = asyncio.create_task(self._do_login())
        return True

    async def _do_login(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            return False

        session = aiohttp.ClientSession(trust_env=True)
        try:
            qr_data = await _api_get(
                session,
                base_url=self._base_url,
                endpoint=EP_GET_QRCODE,
                timeout_ms=10000,
            )
            if qr_data.get("ret") not in (0, None):
                logger.error("wechat_personal: get qrcode failed: %s", qr_data)
                return False
            self._qr_url = qr_data.get("qrcode_url", "")
            logger.info("wechat_personal: QR code URL: %s", self._qr_url)

            poll_count = 0
            timeout = 300
            start = time.time()
            while time.time() - start < timeout:
                poll_count += 1
                await asyncio.sleep(3)
                try:
                    check = await _api_get(
                        session,
                        base_url=self._base_url,
                        endpoint=EP_CHECK_QRCODE,
                        timeout_ms=10000,
                    )
                except Exception as exc:
                    logger.warning("wechat_personal: check qrcode error: %s", exc)
                    continue

                status = check.get("status", "")
                if status == "success":
                    self._token = check.get("token", "")
                    self._account_id = check.get("account_id", "")
                    self._user_id = check.get("user_id", "")
                    logger.info("wechat_personal: login successful, account_id=%s", self._account_id)
                    _save_credentials(self._account_id, self._token, self._base_url, self._user_id)
                    await self._connect_from_session(session)
                    return True
                elif status == "expired":
                    logger.warning("wechat_personal: QR code expired, refreshing...")
                    try:
                        qr_data = await _api_get(
                            session,
                            base_url=self._base_url,
                            endpoint=EP_GET_QRCODE,
                            timeout_ms=10000,
                        )
                        if qr_data.get("ret") in (0, None):
                            self._qr_url = qr_data.get("qrcode_url", "")
                            logger.info("wechat_personal: new QR code URL: %s", self._qr_url)
                    except Exception as e:
                        logger.error("wechat_personal: refresh QR failed: %s", e)
                elif status == "scanned":
                    logger.info("wechat_personal: QR scanned, waiting for confirm...")
                elif status == "waiting":
                    pass
                else:
                    logger.debug("wechat_personal: QR status=%s", status)

            logger.error("wechat_personal: login timeout")
            return False
        finally:
            if not self._running:
                await session.close()

    async def _connect(self) -> None:
        if not self._account_id or not self._token:
            return
        try:
            logger.info("wechat_personal: _connect starting, account=%s", self._account_id[:8])
            self._session = aiohttp.ClientSession(trust_env=True)
            logger.info("wechat_personal: _connect: session created")

            # 不再单独调用 _verify_credentials（会消耗消息）
            # 直接验证 token 有效性：用短超时发一个 get_updates，如果 ret!=0 说明 token 无效
            # 但不使用空 sync_buf，而是用保存的 sync_buf（避免消费消息）
            saved_buf = _load_sync_buf(self._account_id)
            logger.info("wechat_personal: _connect: saved sync_buf=%s", saved_buf[:30] if saved_buf else "(empty)")

            # 用短超时验证 token，sync_buf 用空字符串避免消费已有消息
            verify_resp = await _get_updates(
                self._session,
                base_url=self._base_url,
                token=self._token,
                sync_buf="",
                timeout_ms=5000,
            )
            logger.info("wechat_personal: _connect: verify response: %s",
                       json.dumps(verify_resp, ensure_ascii=False)[:300])
            ret = verify_resp.get("ret", 0)
            errcode = verify_resp.get("errcode", 0)
            if ret not in {0, None} or errcode not in {0, None}:
                raise RuntimeError(f"credentials invalid: ret={ret} errcode={errcode}")
            logger.info("wechat_personal: _connect: credentials verified OK")

            # 如果验证响应中包含消息，直接处理
            verify_msgs = verify_resp.get("msgs") or verify_resp.get("messages") or []
            if verify_msgs:
                logger.info("wechat_personal: _connect: %d messages in verify response, processing",
                           len(verify_msgs))
                for msg in verify_msgs:
                    logger.info("wechat_personal: verify msg: %s",
                               json.dumps(msg, ensure_ascii=False)[:500])
                    task = asyncio.create_task(self._handle_message(msg))
                    self._msg_tasks.add(task)
                    task.add_done_callback(self._msg_tasks.discard)

            # 如果验证响应返回了新的 sync_buf，使用它作为轮询起点
            new_buf = verify_resp.get("get_updates_buf", "")
            if new_buf:
                logger.info("wechat_personal: _connect: using new sync_buf from verify: %s", new_buf[:30])
                self._initial_sync_buf = new_buf
            else:
                # 使用保存的 sync_buf，但如果太旧（超过1小时）就清空重新开始
                self._initial_sync_buf = saved_buf

            self._running = True
            self._last_heartbeat = time.time()
            logger.info("wechat_personal: _connect: _running=True, creating poll task")
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("wechat_personal: _connect: poll task created: %s, done=%s, cancelled=%s",
                       self._poll_task, self._poll_task.done(), self._poll_task.cancelled())
            await asyncio.sleep(0)
            logger.info("wechat_personal: _connect: after yield, poll task done=%s, cancelled=%s",
                       self._poll_task.done(), self._poll_task.cancelled())
            logger.info("wechat_personal: connected to %s", self._base_url)
        except Exception as exc:
            logger.error("wechat_personal: _connect failed: %s", exc, exc_info=True)
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _connect_from_session(self, session: "aiohttp.ClientSession") -> None:
        self._session = session
        self._running = True
        self._last_heartbeat = time.time()
        self._initial_sync_buf = ""  # 新登录，从头开始
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("wechat_personal: connected to %s", self._base_url)

    async def _disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _poll_loop(self) -> None:
        logger.info("wechat_personal: poll loop started, account=%s, base=%s",
                   self._account_id[:8], self._base_url)
        sync_buf = getattr(self, '_initial_sync_buf', '') or _load_sync_buf(self._account_id)
        logger.info("wechat_personal: initial sync_buf=%s", sync_buf[:50] if sync_buf else "(empty)")
        consecutive_errors = 0
        poll_count = 0

        while self._running:
            try:
                poll_count += 1
                self._last_heartbeat = time.time()
                logger.info("wechat_personal: poll #%d, waiting for updates (sync_buf=%s)...",
                           poll_count, sync_buf[:30] if sync_buf else "(empty)")

                response = await _get_updates(
                    self._session,
                    base_url=self._base_url,
                    token=self._token,
                    sync_buf=sync_buf,
                    timeout_ms=35000,
                )

                # 记录完整响应（截断到500字符）
                logger.info("wechat_personal: poll #%d response: %s",
                           poll_count, json.dumps(response, ensure_ascii=False)[:500])

                ret = response.get("ret", 0)
                if ret not in (0, None):
                    errcode = response.get("errcode", 0)
                    logger.warning("wechat_personal: get_updates ret=%s errcode=%s", ret, errcode)
                    consecutive_errors += 1
                    if consecutive_errors > 5:
                        logger.error("wechat_personal: too many errors, reconnecting...")
                        await asyncio.sleep(5)
                        consecutive_errors = 0
                    continue

                consecutive_errors = 0
                new_buf = response.get("get_updates_buf", "")
                if new_buf and new_buf != sync_buf:
                    sync_buf = new_buf
                    _save_sync_buf(self._account_id, sync_buf)
                    logger.info("wechat_personal: sync_buf updated to %s", sync_buf[:30])

                # 尝试多种可能的消息字段名
                msgs = response.get("msgs") or response.get("messages") or response.get("msg_list") or []
                if msgs:
                    logger.info("wechat_personal: received %d message(s)", len(msgs))
                    for msg in msgs:
                        logger.info("wechat_personal: raw message: %s", json.dumps(msg, ensure_ascii=False)[:500])
                        task = asyncio.create_task(self._handle_message(msg))
                        self._msg_tasks.add(task)
                        task.add_done_callback(self._msg_tasks.discard)
                else:
                    logger.info("wechat_personal: poll #%d: no messages (response keys=%s)",
                               poll_count, list(response.keys()))

                await asyncio.sleep(0)

            except asyncio.CancelledError:
                logger.info("wechat_personal: poll loop cancelled")
                break
            except Exception as exc:
                logger.error("wechat_personal: poll loop error: %s", exc, exc_info=True)
                consecutive_errors += 1
                await asyncio.sleep(min(2 ** consecutive_errors, 30))

        logger.info("wechat_personal: poll loop exited")

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        try:
            msg_type = msg.get("msg_type", 0)
            from_id = msg.get("from_id", "")
            content = msg.get("content", "")

            logger.info("wechat_personal: message from %s (type=%s): %s",
                       from_id, msg_type, content[:200] if isinstance(content, str) else str(content)[:200])

            if msg_type != MSG_TYPE_TEXT:
                logger.debug("wechat_personal: skipping non-text message type=%s", msg_type)
                return

            text = str(content).strip()
            if not text or not from_id:
                return

            if self._allowed_users:
                if from_id not in self._allowed_users:
                    logger.debug("wechat_personal: blocked message from %s", from_id)
                    return

            session_id = f"wechat-{from_id}"
            logger.info("wechat_personal: publishing external_message, session_id=%s", session_id)
            event = Event("external_message", {
                "source": "wechat",
                "session_id": session_id,
                "text": text,
                "chat_id": from_id,
            })
            self.bus.publish(event)
            logger.info("wechat_personal: external_message published OK")

            # 启动心跳：如果 10 秒后还没回复，自动发"正在处理..."
            self._start_heartbeat(from_id)
        except Exception as exc:
            logger.error("wechat_personal: _handle_message error: %s", exc, exc_info=True)

    async def _on_turn_completed(self, event) -> None:
        turn = event.get("turn")
        # 即使 turn 异常也要尝试从 event payload 取 session_id 停心跳，
        # 否则用户会持续收到"正在处理中..."兜底消息。
        session_id = ""
        if turn is not None and hasattr(turn, "session_id"):
            session_id = turn.session_id or ""
        if not session_id:
            session_id = event.get("session_id") or ""
        if not session_id:
            logger.warning(
                "wechat_personal: _on_turn_completed: no session_id, "
                "cannot stop heartbeat (turn=%s, event_keys=%s)",
                type(turn).__name__ if turn else None, list(event.keys()),
            )
            # 兜底：清理所有超时心跳（>90s 的肯定已经跑完或卡死）
            self._cleanup_stale_heartbeats()
            return
        if not session_id.startswith("wechat-"):
            logger.debug("wechat_personal: _on_turn_completed: session_id=%s not wechat", session_id)
            return
        chat_id = session_id[7:]

        # 停止心跳，清理进度状态（无论本次 turn 成功还是失败）
        self._stop_heartbeat(chat_id)

        if turn is not None:
            result = getattr(turn, "result", "") or getattr(turn, "text", "") or getattr(turn, "output", "")
        else:
            result = ""
        logger.info("wechat_personal: _on_turn_completed: chat_id=%s, result=%s, has_result=%s",
                   chat_id[:8], bool(result), hasattr(turn, "result") if turn else False)
        if result:
            logger.info("wechat_personal: turn_completed for %s, result_len=%d",
                       chat_id[:8], len(str(result)))
            await self.send(chat_id, str(result))

    def _cleanup_stale_heartbeats(self) -> None:
        """清理已完成或超时的心跳任务（避免字典残留 + 异常路径泄漏）。"""
        if not self._heartbeat_tasks:
            return
        done_keys = [k for k, t in self._heartbeat_tasks.items() if t.done()]
        for k in done_keys:
            self._heartbeat_tasks.pop(k, None)
            self._progress_last_sent.pop(k, None)
            self._progress_count.pop(k, None)

    # -------------------------------------------------- 实时进度反馈
    async def _on_turn_progress(self, event) -> None:
        """处理 coordinator 发布的进度事件，自动给用户发反馈。"""
        session_id = event.get("session_id") or ""
        if not session_id or not session_id.startswith("wechat-"):
            return
        chat_id = session_id[7:]
        message = event.get("message") or ""
        if not message:
            return

        now = time.time()
        # 速率控制：间隔至少 6 秒，最多 5 条
        last_sent = self._progress_last_sent.get(chat_id, 0)
        count = self._progress_count.get(chat_id, 0)

        if count >= 5:
            return
        if count > 0 and (now - last_sent) < 6:
            return

        # 先发送，成功后才更新计数；失败时保留配额，避免静默消耗完 5 条
        try:
            await self.send(chat_id, f"⏳ {message}")
        except Exception as exc:
            logger.debug("wechat_personal: progress send failed: %s", exc)
            return
        self._progress_last_sent[chat_id] = now
        self._progress_count[chat_id] = count + 1

    def _start_heartbeat(self, chat_id: str) -> None:
        """启动心跳任务：10 秒后还没回复就发"正在处理"，之后每 20 秒一次。"""
        self._stop_heartbeat(chat_id)
        self._progress_last_sent.pop(chat_id, None)
        self._progress_count.pop(chat_id, None)
        task = asyncio.create_task(self._heartbeat_loop(chat_id))
        self._heartbeat_tasks[chat_id] = task

    def _stop_heartbeat(self, chat_id: str) -> None:
        """停止心跳任务并清理状态。"""
        task = self._heartbeat_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
        self._progress_last_sent.pop(chat_id, None)
        self._progress_count.pop(chat_id, None)

    async def _heartbeat_loop(self, chat_id: str) -> None:
        """心跳循环：10 秒后首次提醒，之后每 20 秒一次，最多 3 次。"""
        try:
            await asyncio.sleep(10)
            for i in range(3):
                count = self._progress_count.get(chat_id, 0)
                if count >= 5:
                    return
                now = time.time()
                last_sent = self._progress_last_sent.get(chat_id, 0)
                if now - last_sent < 8:
                    # 刚发过进度消息，跳过这次心跳
                    pass
                else:
                    # 同 _on_turn_progress：先发后计数，失败不消耗配额
                    try:
                        await self.send(chat_id, "⏳ 正在处理中，请稍候...")
                    except Exception:
                        pass
                    else:
                        self._progress_last_sent[chat_id] = now
                        self._progress_count[chat_id] = count + 1
                await asyncio.sleep(20)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("wechat_personal: heartbeat error: %s", exc)
        finally:
            # 任务自然结束（没被 _stop_heartbeat 取消）时从字典移除，
            # 避免 done task 残留 + 进度状态泄漏到下一轮。
            current = asyncio.current_task()
            if current is not None and self._heartbeat_tasks.get(chat_id) is current:
                self._heartbeat_tasks.pop(chat_id, None)

    async def send(self, chat_id: str, text: str) -> bool:
        if not self._running or not self._session:
            logger.warning("wechat_personal: send failed, not running, running=%s session=%s",
                          self._running, self._session is not None)
            return False
        try:
            result = await _send_msg(
                self._session,
                base_url=self._base_url,
                token=self._token,
                chat_id=chat_id,
                content=text,
            )
            ret = result.get("ret", -1)
            success = ret in (0, None)
            logger.info("wechat_personal: send result=%s (ret=%s)", success, ret)
            return success
        except Exception as exc:
            logger.error("wechat_personal: send error: %s", exc)
            return False

    @property
    def qr_url(self) -> str:
        return self._qr_url

    @property
    def running(self) -> bool:
        return self._running

    @property
    def account_id(self) -> str:
        return self._account_id


__all__ = ["WeChatPersonalGateway"]
