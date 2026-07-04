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
import base64
import json
import logging
import os
import random
import struct
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

# iLink 官方协议（参考阿里云/掘金逆向文章 + wechatbot.dev 文档）：
# - GET 请求（获取二维码、轮询扫码状态）不需要 iLink-App-Id
# - iLink-App-ClientVersion 的值应为 "1"（不是 "100000223"）
# - POST 请求需要 Authorization: Bearer <bot_token> + AuthorizationType: ilink_bot_token
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
# 修复：iLink 官方端点名是 get_bot_qrcode / get_qrcode_status / sendmessage。
# 之前用的 getqrcode / checkqrcode / sendmsg 全部返回 HTTP 404。
EP_GET_QRCODE = "ilink/bot/get_bot_qrcode?bot_type=3"
EP_CHECK_QRCODE = "ilink/bot/get_qrcode_status"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MSG = "ilink/bot/sendmessage"

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
    # POST 请求需要 Authorization + AuthorizationType + X-WECHAT-UIN
    # base_info.channel_version 在 payload 中设置（由调用方处理）
    headers = {
        "Content-Type": "application/json",
        "iLink-App-ClientVersion": "1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["AuthorizationType"] = "ilink_bot_token"
        # X-WECHAT-UIN: 随机 4 字节 → uint32 → base64
        uin = struct.pack(">I", random.randint(0, 0xFFFFFFFF))
        headers["X-WECHAT-UIN"] = base64.b64encode(uin).decode()

    # 官方协议：所有 POST 请求体需包含 base_info.channel_version
    if "base_info" not in payload:
        payload = {**payload, "base_info": {"channel_version": "2.0.0"}}

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
    # GET 请求（获取二维码、轮询扫码状态）不需要 iLink-App-Id
    # 官方协议：iLink-App-ClientVersion 值为 "1"
    headers = {
        "iLink-App-ClientVersion": "1",
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
    context_token: str = "",
    client_id: str = "",
    msg_type: int = MSG_TYPE_TEXT,
) -> Dict[str, Any]:
    # iLink 官方协议发送消息格式：
    # { "msg": { "to_user_id": ..., "context_token": ..., "client_id": ...,
    #            "item_list": [{"type": 1, "text_item": {"text": ...}}] } }
    # context_token 和 client_id 都来自入站消息，回复时原样回传。
    # 之前误以为 client_id = context_token（值相同），实际是两个不同字段。
    # 两个都传以覆盖 iLink 协议要求（HTTP 200 但消息静默丢弃是已知坑）。
    msg_obj: Dict[str, Any] = {
        "to_user_id": chat_id,
        "item_list": [{"type": msg_type, "text_item": {"text": content}}],
    }
    if context_token:
        msg_obj["context_token"] = context_token
    if client_id:
        msg_obj["client_id"] = client_id
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MSG,
        payload={"msg": msg_obj},
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
        # iLink 二维码 token，用于轮询扫码状态
        self._qrcode_token: str = ""
        self._msg_tasks: set = set()
        # 按用户缓存 context_token 和 client_id（回复时必须回传）。
        # 修复：入站消息的 context_token 和 client_id 是两个不同的值，
        # 之前误以为是同一个（注释"client_id = context_token"是错的）。
        # iLink 可能要求发送时传其中一个或两个，故分别缓存。
        self._context_tokens: Dict[str, str] = {}
        self._client_ids: Dict[str, str] = {}
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
                # 修复：旧版本保存的凭据文件可能没有 account_id 字段
                # （或为空字符串），导致 _connect() 因 self._account_id 为空
                # 而静默跳过，微信网关不工作。从文件名推断 account_id
                # （_save_credentials 用 _sanitize_chat_id(account_id) 作
                # 文件名，sanitized 后 alnum/@.-_ 都保留，对于常见 account_id
                # 如 "c147268ca92c@im.bot" 反向提取无损）。
                if not data.get("account_id"):
                    data["account_id"] = path.stem
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
        # 如果登录 task 还在运行（后台轮询扫码状态），直接返回。
        # /微信 skill 会读取最新的 self._qr_url——如果二维码已自动刷新，
        # 这里返回后 skill 会显示新的 URL，用户无需等待。
        if self._login_task is not None and not self._login_task.done():
            return True
        # 旧的登录 task 已结束（超时/失败），清空旧二维码重新开始
        self._qr_url = ""
        self._qrcode_token = ""
        self._login_task = None
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
        # 同步等待 QR URL 就绪：API 超时 10 秒，这里等 13 秒（130 × 0.1s）
        # 确保 /微信 命令返回时已能展示二维码（修复"第一次不显示二维码"问题）
        for _ in range(130):
            if self._qr_url:
                break
            if self._login_task.done():
                # _do_login 已结束（可能失败或成功），不再等
                break
            await asyncio.sleep(0.1)
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
            # iLink 官方响应字段：qrcode（用于轮询）+ qrcode_img_content（扫码图片 URL）
            self._qrcode_token = qr_data.get("qrcode", "")
            self._qr_url = qr_data.get("qrcode_img_content", "")
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
                        endpoint=f"{EP_CHECK_QRCODE}?qrcode={self._qrcode_token}",
                        timeout_ms=10000,
                    )
                except Exception as exc:
                    logger.warning("wechat_personal: check qrcode error: %s", exc)
                    continue

                status = check.get("status", "")
                if status == "confirmed":
                    # iLink 官方协议 confirmed 响应字段：
                    # bot_token, ilink_bot_id, ilink_user_id, baseurl
                    # 兼容旧字段名 token/account_id/user_id
                    self._token = check.get("bot_token", "") or check.get("token", "")
                    self._account_id = check.get("ilink_bot_id", "") or check.get("account_id", "")
                    self._user_id = check.get("ilink_user_id", "") or check.get("user_id", "")
                    # baseurl 可能与默认值不同，始终使用返回值
                    returned_baseurl = check.get("baseurl", "")
                    if returned_baseurl:
                        self._base_url = returned_baseurl
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
                            self._qrcode_token = qr_data.get("qrcode", "")
                            self._qr_url = qr_data.get("qrcode_img_content", "")
                            logger.info("wechat_personal: new QR code URL: %s", self._qr_url)
                    except Exception as e:
                        logger.error("wechat_personal: refresh QR failed: %s", e)
                elif status == "scaned":
                    logger.info("wechat_personal: QR scanned, waiting for confirm...")
                elif status == "wait":
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
            # 修复：之前这里静默 return，导致 setup 看似成功但微信网关
            # 没真正连接，用户输入消息无响应也无日志可查。打 warning 让
            # 问题立即可见（常见原因：saved 凭据文件缺 account_id 字段）。
            logger.warning("wechat_personal: _connect skipped (account_id=%s, token=%s)",
                           bool(self._account_id), bool(self._token))
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
            # 修复：如果 _connect 失败是因为 token 过期（errcode=-14），
            # 删除本地凭据文件，避免下次启动又尝试用过期 token 连接
            # （导致用户每次启动都看到 ERROR 但没机会重新扫码）。
            if "-14" in str(exc) and self._account_id:
                from pathlib import Path as _Path
                for fname in (f"{self._account_id}.json", f"{self._account_id}.sync.json"):
                    fpath = _Path(DATA_DIR) / fname
                    if fpath.exists():
                        try:
                            fpath.unlink()
                            logger.info("wechat_personal: removed expired credentials file %s", fname)
                        except OSError:
                            pass

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
                # 修复：errcode=-14 (session timeout) 是 token 过期的标志。
                # 之前代码只检查 ret 字段，但 ercode=-14 的响应里可能没有
                # ret 字段（或 ret=0），导致被当作"成功但无消息"处理，
                # poll loop 无限循环用过期 token 轮询。现在检测到 -14
                # 时断开连接、清除凭据、设 _running=False，让下次 /微信
                # 自动触发重新扫码登录。
                errcode = response.get("errcode", 0)
                if errcode == -14:
                    logger.warning("wechat_personal: session expired (errcode=-14), clearing credentials "
                                   "and disconnecting — user must re-login with /微信")
                    # 先保存凭据文件路径（在清空 _account_id 之前）
                    saved_account = self._account_id
                    from pathlib import Path as _Path
                    cred_file = _Path(DATA_DIR) / f"{saved_account}.json" if saved_account else None
                    sync_file = _Path(DATA_DIR) / f"{saved_account}.sync.json" if saved_account else None
                    self._running = False
                    self._token = ""
                    self._account_id = ""
                    # 不能调 self._disconnect()——我们就在 _poll_loop 里，
                    # _disconnect() 会 cancel self._poll_task 然后 await 它，
                    # 等于自己等自己，死锁。直接关闭 session。
                    if self._session and not self._session.closed:
                        await self._session.close()
                    self._session = None
                    self._poll_task = None
                    # 删除过期的凭据文件，避免下次启动又尝试连接
                    for fpath in (cred_file, sync_file):
                        if fpath and fpath.exists():
                            try:
                                fpath.unlink()
                                logger.info("wechat_personal: removed expired file %s", fpath.name)
                            except OSError:
                                pass
                    break
                if ret not in (0, None):
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
                        logger.info("wechat_personal: raw message: %s", json.dumps(msg, ensure_ascii=False)[:1000])
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
            # iLink 官方协议字段名：
            # from_user_id（不是 from_id）
            # message_type（不是 msg_type）
            # item_list[0].text_item.text（不是 content）
            # context_token 和 client_id 是两个不同的值，都要缓存：
            # - context_token: 会话上下文 token（AARzJWAFAAAB... 开头）
            # - client_id: 消息投递标识（mmassistant_bypmsg_inbox_... 开头）
            msg_type = msg.get("message_type", 0) or msg.get("msg_type", 0)
            from_id = msg.get("from_user_id", "") or msg.get("from_id", "")
            context_token = msg.get("context_token", "")
            client_id = msg.get("client_id", "")

            # 从 item_list 提取文本
            text = ""
            item_list = msg.get("item_list", [])
            if item_list:
                for item in item_list:
                    if item.get("type") == MSG_TYPE_TEXT:
                        text_item = item.get("text_item", {})
                        text = text_item.get("text", "")
                        if text:
                            break

            logger.info("wechat_personal: message from %s (type=%s): %s",
                       from_id, msg_type, text[:200] if isinstance(text, str) else str(text)[:200])

            if msg_type != MSG_TYPE_TEXT:
                logger.debug("wechat_personal: skipping non-text message type=%s", msg_type)
                return

            text = str(text).strip()
            if not text or not from_id:
                return

            # 缓存 context_token 和 client_id（回复时必须回传）
            if context_token:
                self._context_tokens[from_id] = context_token
            if client_id:
                self._client_ids[from_id] = client_id
            logger.debug(
                "wechat_personal: cached for %s: ctx_token=%s, client_id=%s",
                from_id[:20],
                "yes" if context_token else "no",
                "yes" if client_id else "no",
            )

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
            # 从缓存获取 context_token 和 client_id（回复时必须回传）
            context_token = self._context_tokens.get(chat_id, "")
            client_id = self._client_ids.get(chat_id, "")
            if not context_token and not client_id:
                logger.warning("wechat_personal: no context_token/client_id for %s, message may not be delivered", chat_id[:20])
            # 诊断日志：打印发送关键字字段（token 脱敏只显示前 20 字符 + 长度）
            logger.info(
                "wechat_personal: sending to=%s, content=%s, ctx_token=%s(len=%d), client_id=%s(len=%d)",
                chat_id[:20],
                (text[:50] + "...") if len(text) > 50 else text,
                (context_token[:20] + "...") if context_token else "EMPTY",
                len(context_token),
                (client_id[:20] + "...") if client_id else "EMPTY",
                len(client_id),
            )
            result = await _send_msg(
                self._session,
                base_url=self._base_url,
                token=self._token,
                chat_id=chat_id,
                content=text,
                context_token=context_token,
                client_id=client_id,
            )
            ret = result.get("ret", 0)
            errcode = result.get("errcode", 0)
            # iLink sendmessage 成功响应通常是 {} 空对象（无 ret 字段）
            # 或 ret=0。errcode != 0 表示错误。
            success = (ret in (0, None)) and (errcode in (0, None))
            logger.info("wechat_personal: send result=%s (ret=%s, errcode=%s, response=%s)",
                       success, ret, errcode, json.dumps(result, ensure_ascii=False)[:200])
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
