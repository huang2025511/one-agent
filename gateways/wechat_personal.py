"""Personal WeChat (个人微信) gateway — using itchat-uos.

This gateway logs into a **personal** WeChat account (not an Official Account /
not a Mini Program), scans the QR code displayed in terminal, and then listens
for private messages.

Requirements
------------
``pip install itchat-uos`` (recommended) or ``pip install itchat`` (legacy).

``itchat-uos`` patches the old itchat to work with UOS protocol, which is what
current WeChat Desktop/Web versions use.

Security Warning
----------------
This gateway runs on a *personal* WeChat account.  WeChat's Terms of Service
prohibit automated messaging on personal accounts (see §10.5).  Use at your
own risk.  The authors recommend using the WeCom (企业微信) gateway for
production.

Usage
-----
1. ``pip install one-agent[wechat]`` or ``pip install itchat-uos``
2. Enable in config:

.. code-block:: yaml

   gateways:
     wechat_personal:
       enabled: true
       allowed_users: []       # empty = allow all
       reply_prefix: "[Bot] "  # prefix auto-replies
       hot_reload: false       # True = watch .pkl for QR changes

3. Start one-agent — a QR code will appear in terminal.
4. Scan with WeChat mobile app.
5. Agent listens for private messages and answers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from core.plugin import Plugin

logger = logging.getLogger(__name__)

# ---- Pending sessions: we need to wait for the coordinator to reply ----
_REPLY_TIMEOUT = 120.0  # seconds


class WeChatPersonalGateway(Plugin):
    """个人微信网关 — 登录个人微信号，收发消息。

    使用 itchat-uos 库（基于 UOS 协议绕过 Web WeChat 限制）。
    通过 ``itchat.auto_login()`` 弹出二维码供手机扫描。
    """

    name = "gateway_wechat_personal"

    def __init__(self) -> None:
        super().__init__()
        self._allowed_users: List[str] = []        # RemarkName or NickName
        self._reply_prefix: str = ""               # e.g. "[Bot] "
        self._hot_reload: bool = False
        self._enabled: bool = False
        self._itchat = None
        self._task: Optional[asyncio.Task] = None
        self._replies: Dict[str, str] = {}
        self._pending: Dict[str, asyncio.Event] = {}
        self._logged_in: bool = False
        self._own_user_name: str = ""

    # ------------------------------------------------------------ lifecycle
    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("gateways") or {}).get("wechat_personal") or {}
        self._enabled = bool(cfg.get("enabled", False))
        if not self._enabled:
            logger.info("wechat_personal disabled")
            return
        self._allowed_users = [str(u).strip() for u in (cfg.get("allowed_users") or []) if str(u).strip()]
        self._reply_prefix = str(cfg.get("reply_prefix", ""))
        self._hot_reload = bool(cfg.get("hot_reload", False))

        # Check dependency
        try:
            import itchat  # type: ignore  # noqa: F401
        except ImportError as exc:
            logger.warning(
                "itchat / itchat-uos not installed. "
                "Run: pip install itchat-uos.  Skipping personal WeChat gateway."
            )
            logger.debug("import error: %s", exc)
            return

        self.bus.subscribe("turn_completed", self._on_done)
        self._task = asyncio.create_task(self._run())
        logger.info("wechat_personal enabled (personal WeChat account mode)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._itchat:
            try:
                self._itchat.logout()
            except Exception:  # noqa: BLE001
                pass
        await super().stop()

    # ------------------------------------------------------------ helpers
    async def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None:
            return
        sid = turn.session_id
        if sid in self._pending:
            self._replies[sid] = turn.result or f"[error: {turn.error}]"
            self._pending[sid].set()

    async def _send_text(self, to_user_name: str, text: str) -> bool:
        """Send a text message via itchat (runs in thread since itchat is sync)."""
        if self._itchat is None:
            return False
        full = f"{self._reply_prefix}{text}" if self._reply_prefix else text
        try:
            result = await asyncio.to_thread(
                self._itchat.send, full[:2000], toUserName=to_user_name
            )
            return result is not None and str(result).lower() != "false"
        except Exception as exc:  # noqa: BLE001
            logger.warning("wechat_personal send error: %s", exc)
            return False

    # ------------------------------------------------------------ main loop
    async def _run(self) -> None:
        """Run itchat in a side thread, bridge messages to the event bus."""
        import itchat  # type: ignore
        from itchat.content import TEXT  # type: ignore

        self._itchat = itchat

        # ---- register message handler (runs in itchat's internal thread) ----
        @itchat.msg_register(TEXT, isFriendChat=True, isGroupChat=False)
        def _on_text(msg: Any) -> None:
            """Called by itchat's internal thread.  Must NOT block.

            We schedule a coroutine on the asyncio event loop to handle the
            message asynchronously.
            """
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(
                self._handle_message(msg), loop
            )

        # ---- login (this blocks!) ----
        logger.info("wechat_personal: launching QR code login...")
        try:
            await asyncio.to_thread(
                self._itchat.auto_login,
                hotReload=self._hot_reload,
                enableCmdQR=2,  # simplified QR code (good for terminal)
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("wechat_personal login failed: %s", exc)
            return

        self._logged_in = True
        self._own_user_name = self._itchat.instance.storageClass.userName or ""
        logger.info("wechat_personal: logged in as %s", self._own_user_name)

        # ---- run itchat's internal event loop in thread ----
        try:
            await asyncio.to_thread(self._itchat.run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wechat_personal loop exited: %s", exc)

    # ------------------------------------------------------------ message handling
    async def _handle_message(self, msg: Any) -> None:
        """Process a single text message from WeChat."""
        from_user = getattr(msg, "FromUserName", "")
        to_user = getattr(msg, "ToUserName", "")
        text_raw = getattr(msg, "Text", "") or getattr(msg, "Content", "") or ""
        text = str(text_raw).strip()

        if not text or not from_user or from_user == self._own_user_name:
            return

        # Resolve sender identity
        sender_name = ""
        sender_remark = ""
        sender_nick = ""
        try:
            sender = getattr(msg, "User", None) or {}
            sender_nick = getattr(sender, "NickName", "") or ""
            sender_remark = getattr(sender, "RemarkName", "") or ""
            sender_name = sender_remark or sender_nick
        except Exception:  # noqa: BLE001
            pass

        # Check allowed_users filter
        if self._allowed_users:
            if (sender_nick not in self._allowed_users and
                sender_remark not in self._allowed_users and
                from_user not in self._allowed_users):
                logger.debug("wechat_personal: blocked message from %s", sender_name)
                return

        msg_key = f"wxp-{from_user}-{time.time_ns()}"
        evt = asyncio.Event()
        self._pending[msg_key] = evt

        if self.bus is not None:
            self.bus.publish({
                "type": "external_message",
                "source": "wechat_personal",
                "session_id": msg_key,
                "text": text,
                "chat_id": from_user,
            })

        # Wait for coordinator reply
        try:
            await asyncio.wait_for(evt.wait(), timeout=_REPLY_TIMEOUT)
            reply = self._replies.get(msg_key, "[no reply]")
            await self._send_text(from_user, reply)
        except asyncio.TimeoutError:
            await self._send_text(from_user, "抱歉，处理超时，请稍后再试。")
        finally:
            self._pending.pop(msg_key, None)
            self._replies.pop(msg_key, None)


__all__ = ["WeChatPersonalGateway"]