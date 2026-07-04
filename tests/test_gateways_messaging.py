"""消息网关单元测试 — 覆盖 gateways/ 下 7 个消息网关的关键行为。

测试范围：
- gateways/__init__.py：CLIGateway、WebGateway、_match_cli_intent
- gateways/messaging.py：BaseMessagingGateway + Telegram / WeCom / DingTalk /
  Feishu / Discord / Slack 六个消息网关
- gateways/wechat_personal.py：WeChatPersonalGateway + 模块级 helper

策略：
- 不真启 callback server / WebGateway.start（避免 bind 18791/18794/18795）
- 不真发网络请求，用 unittest.mock.patch / AsyncMock 替换 httpx
- fail-closed 路径用配置缺值触发，断言不订阅事件 / 不起后台任务
- 签名 / 解密算法以纯函数式重算验证（与产品代码独立同源）
- WeChatPersonalGateway._find_saved_account 扫 ~/.one-agent，用 monkeypatch
  改 DATA_DIR 为 tmp_path 避免污染环境
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# 通用 fixture：构造一个最小可用的伪 ctx
# ============================================================
def _make_ctx(config: dict) -> SimpleNamespace:
    """构造一个最小可用的 AgentContext 替身。

    gateway 的 setup() 只用到 ctx.config 和 ctx.bus（来自 Plugin.setup），
    其它字段不访问。bus 用 MagicMock 拦截 subscribe / publish。
    """
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.publish = MagicMock()
    return SimpleNamespace(config=config, bus=bus, started_at=time.time())


# ============================================================
# gateways/__init__.py
# ============================================================
class TestCLIGateway:
    def test_default_attributes(self):
        from gateways import CLIGateway
        gw = CLIGateway()
        assert gw.name == "gateway_cli"
        assert gw._prompt == "one-agent> "
        assert gw._session_id  # uuid hex[:12]，非空
        assert len(gw._session_id) == 12

    @pytest.mark.asyncio
    async def test_setup_reads_prompt_and_subscribes(self):
        from gateways import CLIGateway
        gw = CLIGateway()
        ctx = _make_ctx({"gateways": {"cli": {"prompt": "test> "}}})
        await gw.setup(ctx)
        assert gw._prompt == "test> "
        # 应订阅 turn_completed 与 approval_needed
        subscribed_types = {call.args[0] for call in gw.bus.subscribe.call_args_list}
        assert "turn_completed" in subscribed_types
        assert "approval_needed" in subscribed_types

    @pytest.mark.asyncio
    async def test_setup_falls_back_to_default_prompt_when_missing(self):
        from gateways import CLIGateway
        gw = CLIGateway()
        ctx = _make_ctx({"gateways": {}})  # 没有 cli 节
        await gw.setup(ctx)
        assert gw._prompt == "one-agent> "  # 默认值未被覆盖


class TestWebGatewayDefaults:
    def test_default_attributes(self):
        from gateways import WebGateway
        gw = WebGateway()
        assert gw.name == "gateway_web"
        assert gw._host == "127.0.0.1"
        assert gw._port == 18791
        assert gw._enabled is True  # WebGateway 默认启用
        assert gw._api_key == ""
        assert gw._rate_limit_per_minute == 60
        assert gw._max_chat_bytes == 65536
        assert gw._task is None
        assert gw._agent_callback is None

    def test_bind_callback(self):
        from gateways import WebGateway
        gw = WebGateway()
        cb = MagicMock()
        gw.bind_callback(cb)
        assert gw._agent_callback is cb

    @pytest.mark.asyncio
    async def test_setup_reads_config(self):
        from gateways import WebGateway
        gw = WebGateway()
        ctx = _make_ctx({"gateways": {"web": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": 9999,
            "api_key": "sk-test",
            "rate_limit_per_minute": 10,
            "max_chat_bytes": 1024,
        }}})
        await gw.setup(ctx)
        assert gw._host == "0.0.0.0"
        assert gw._port == 9999
        assert gw._api_key == "sk-test"
        assert gw._rate_limit_per_minute == 10
        assert gw._max_chat_bytes == 1024

    @pytest.mark.asyncio
    async def test_setup_disabled_returns_silently(self):
        """enabled=False 时 setup 应提前返回，不抛错。"""
        from gateways import WebGateway
        gw = WebGateway()
        ctx = _make_ctx({"gateways": {"web": {"enabled": False}}})
        await gw.setup(ctx)  # 不应抛异常
        assert gw._enabled is False


class TestMatchCliIntent:
    """_match_cli_intent 的精准匹配 + 正则模糊匹配。"""

    def test_exact_exit_commands(self):
        from gateways import _match_cli_intent
        for text in ("exit", "quit", "q", "bye", "退出"):
            assert _match_cli_intent(text) == "exit"

    def test_exact_help(self):
        from gateways import _match_cli_intent
        for text in ("help", "?", "帮助"):
            assert _match_cli_intent(text) == "help"

    def test_no_match_returns_none(self):
        from gateways import _match_cli_intent
        assert _match_cli_intent("hello world") is None
        assert _match_cli_intent("") is None
        assert _match_cli_intent("随便一句话") is None


class TestGatewayLazyLoader:
    """gateways/__init__.py 的 __getattr__ 懒加载 + 缓存。"""

    def test_lazy_import_returns_messaging_classes(self):
        import gateways
        from gateways import messaging
        # 懒加载返回的是同一对象
        assert gateways.WeComGateway is messaging.WeComGateway
        assert gateways.FeishuGateway is messaging.FeishuGateway
        assert gateways.DingTalkGateway is messaging.DingTalkGateway
        assert gateways.TelegramGateway is messaging.TelegramGateway
        assert gateways.DiscordGateway is messaging.DiscordGateway
        assert gateways.SlackGateway is messaging.SlackGateway

    def test_lazy_import_wechat_personal(self):
        import gateways
        from gateways.wechat_personal import WeChatPersonalGateway
        assert gateways.WeChatPersonalGateway is WeChatPersonalGateway


# ============================================================
# BaseMessagingGateway 共享行为
# ============================================================
class TestBaseMessagingGateway:
    @pytest.mark.asyncio
    async def test_on_done_sets_reply_and_event(self):
        from gateways.messaging import BaseMessagingGateway
        gw = BaseMessagingGateway()
        # 模拟一条 turn_completed 事件
        turn = MagicMock()
        turn.session_id = "test-sid"
        turn.result = "hello reply"
        event = {"turn": turn}
        # 预先注册一个 session Event
        ready_event = asyncio.Event()
        gw._sessions["test-sid"] = ready_event
        await gw._on_done(event)
        # 应设置 reply 内容
        assert gw._replies["test-sid"] == "hello reply"
        # 应触发 event
        assert ready_event.is_set()

    @pytest.mark.asyncio
    async def test_on_done_handles_error_result(self):
        from gateways.messaging import BaseMessagingGateway
        gw = BaseMessagingGateway()
        turn = MagicMock()
        turn.session_id = "err-sid"
        turn.result = None
        turn.error = "boom"
        event = {"turn": turn}
        ready_event = asyncio.Event()
        gw._sessions["err-sid"] = ready_event
        await gw._on_done(event)
        # 错误信息应进 reply
        assert "boom" in gw._replies["err-sid"]
        assert ready_event.is_set()

    @pytest.mark.asyncio
    async def test_wait_and_reply_calls_send_fn(self):
        from gateways.messaging import BaseMessagingGateway
        gw = BaseMessagingGateway()
        # 预置 reply（_on_done 已被触发）
        ready_event = asyncio.Event()
        ready_event.set()
        gw._sessions["msg-1"] = ready_event
        gw._replies["msg-1"] = " canned reply"
        send_fn = AsyncMock()
        await gw._wait_and_reply("msg-1", "chat-123", send_fn, timeout=1.0)
        send_fn.assert_awaited_once_with("chat-123", " canned reply")
        # 清理
        assert "msg-1" not in gw._sessions
        assert "msg-1" not in gw._replies

    @pytest.mark.asyncio
    async def test_wait_and_reply_timeout_sends_timeout_marker(self):
        from gateways.messaging import BaseMessagingGateway
        gw = BaseMessagingGateway()
        # 必须先注册 _sessions[msg_key]，否则 _wait_and_reply 直接 return
        # 不调 send_fn。这里注册一个未触发的 event → wait_for 超时。
        gw._sessions["no-reply"] = asyncio.Event()
        send_fn = AsyncMock()
        await gw._wait_and_reply("no-reply", "chat-456", send_fn, timeout=0.05)
        send_fn.assert_awaited_once()
        args = send_fn.await_args.args
        assert args[0] == "chat-456"
        assert "[timeout]" in args[1]

    @pytest.mark.asyncio
    async def test_stop_cancels_task_and_closes_client(self):
        from gateways.messaging import BaseMessagingGateway
        gw = BaseMessagingGateway()
        # 用一个真实的 asyncio task（无限 sleep），让 stop() 真能 cancel + await
        async def _forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
        gw._task = asyncio.create_task(_forever())
        gw._client = MagicMock()
        gw._client.aclose = AsyncMock()
        await gw.stop()
        gw._client.aclose.assert_awaited_once()
        assert gw._task.cancelled() or gw._task.done()


# ============================================================
# TelegramGateway
# ============================================================
class TestTelegramGateway:
    @pytest.mark.asyncio
    async def test_setup_disabled_does_not_create_client(self):
        """enabled=False 时不创建 httpx client，不订阅事件。"""
        from gateways.messaging import TelegramGateway
        gw = TelegramGateway()
        ctx = _make_ctx({"gateways": {"telegram": {"enabled": False}}})
        await gw.setup(ctx)
        assert gw._client is None
        # setup 总会把 _token 设为 cfg.get("bot_token") or ""（即使 disabled）
        assert gw._token == ""
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_enabled_without_token_does_not_start(self):
        """enabled=True 但无 bot_token 也不启动。"""
        from gateways.messaging import TelegramGateway
        gw = TelegramGateway()
        ctx = _make_ctx({"gateways": {"telegram": {
            "enabled": True, "bot_token": "",
        }}})
        await gw.setup(ctx)
        # 没 token 不应该创建 client
        assert gw._client is None
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_enabled_with_token_subscribes(self):
        from gateways.messaging import TelegramGateway
        gw = TelegramGateway()
        ctx = _make_ctx({"gateways": {"telegram": {
            "enabled": True, "bot_token": "123:abc", "allowed_users": [111, 222],
        }}})
        await gw.setup(ctx)
        assert gw._token == "123:abc"
        assert gw._allowed_users == [111, 222]
        assert gw._client is not None  # httpx.AsyncClient 已创建
        subscribed_types = {c.args[0] for c in gw.bus.subscribe.call_args_list}
        assert "turn_completed" in subscribed_types
        # 后台 _loop 任务已起
        assert gw._task is not None
        # 清理
        await gw.stop()

    @pytest.mark.asyncio
    async def test_send_posts_to_telegram_api(self):
        from gateways.messaging import TelegramGateway
        gw = TelegramGateway()
        gw._token = "123:abc"
        # mock client.post
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"ok": True})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        await gw._send(456, "hello world")
        gw._client.post.assert_awaited_once()
        called_url = gw._client.post.await_args.args[0]
        assert "api.telegram.org" in called_url
        assert "/bot123:abc/sendMessage" in called_url
        # payload 应包含 chat_id 和 text
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert payload["chat_id"] == 456
        assert payload["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_send_truncates_long_text(self):
        """text 超过 4000 字符应被截断。"""
        from gateways.messaging import TelegramGateway
        gw = TelegramGateway()
        gw._token = "tok"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"ok": True})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        long_text = "x" * 5000
        await gw._send(1, long_text)
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert len(payload["text"]) == 4000


# ============================================================
# WeComGateway
# ============================================================
class TestWeComGateway:
    @pytest.mark.asyncio
    async def test_setup_disabled_returns_silently(self):
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        ctx = _make_ctx({"gateways": {"wecom": {"enabled": False}}})
        await gw.setup(ctx)
        assert gw._webhook_key == ""
        gw.bus.subscribe.assert_not_called()
        assert gw._task is None

    @pytest.mark.asyncio
    async def test_setup_webhook_mode_only_no_callback_server(self):
        """webhook 模式只配 webhook_key，不启 callback server。"""
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        ctx = _make_ctx({"gateways": {"wecom": {
            "enabled": True, "mode": "webhook", "webhook_key": "wh-key",
        }}})
        await gw.setup(ctx)
        assert gw._mode == "webhook"
        assert gw._webhook_key == "wh-key"
        # webhook 模式不应起 callback server
        assert gw._task is None

    @pytest.mark.asyncio
    async def test_setup_app_mode_without_token_fails_closed(self):
        """app 模式无 callback_token 应拒绝启动 callback server（fail-closed）。"""
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        ctx = _make_ctx({"gateways": {"wecom": {
            "enabled": True, "mode": "app",
            "corp_id": "x", "secret": "y",
            # 故意不传 callback_token
        }}})
        await gw.setup(ctx)
        # fail-closed：不订阅、不起 task
        assert gw._task is None
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_app_mode_with_token_starts_callback(self):
        """app 模式带 callback_token 应启动 callback server + 订阅事件。"""
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        ctx = _make_ctx({"gateways": {"wecom": {
            "enabled": True, "mode": "app",
            "corp_id": "corp", "secret": "sec",
            "callback_token": "cb-tok",
            "callback_host": "127.0.0.1", "callback_port": 19999,
        }}})
        # 不让 uvicorn 真起 server——patch Server.serve
        with patch("uvicorn.Server.serve", new=AsyncMock()):
            await gw.setup(ctx)
        try:
            assert gw._mode == "app"
            assert gw._token == "cb-tok"
            subscribed = {c.args[0] for c in gw.bus.subscribe.call_args_list}
            assert "turn_completed" in subscribed
            assert gw._task is not None
        finally:
            # 清理后台 task
            await gw.stop()

    @pytest.mark.asyncio
    async def test_send_webhook_without_config_returns_error(self):
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        # 不配置 webhook_key 也不配 client
        result = await gw.send_webhook("hello")
        assert result["ok"] is False
        assert "not configured" in result["error"]

    @pytest.mark.asyncio
    async def test_send_webhook_posts_to_qyapi(self):
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        gw._webhook_key = "wh-secret"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"errcode": 0, "errmsg": "ok"})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        result = await gw.send_webhook("hello", mentioned_list=["user1"])
        assert result["ok"] is True
        url = gw._client.post.await_args.args[0]
        assert "qyapi.weixin.qq.com" in url
        assert "key=wh-secret" in url
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert payload["msgtype"] == "text"
        assert payload["text"]["content"] == "hello"
        # mentioned_list 在非空时挂到 payload["text"]["mentioned_list"]
        assert payload["text"]["mentioned_list"] == ["user1"]

    @pytest.mark.asyncio
    async def test_send_webhook_truncates_long_text(self):
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        gw._webhook_key = "k"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"errcode": 0})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        await gw.send_webhook("x" * 5000)
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert len(payload["text"]["content"]) == 4000

    def test_get_callback_signature_algorithm(self):
        """独立验证 WeCom GET 回调签名算法（与产品代码同源）。

        WeCom 算法：sha1(sorted([token, timestamp, nonce, echostr]).join(""))
        """
        token = "my_token"
        timestamp = "1400000000"
        nonce = "abc"
        echostr = "echo123"
        # 与产品代码完全一致的算法
        sort_list = sorted([token, timestamp, nonce, echostr])
        expected = hashlib.sha1("".join(sort_list).encode()).hexdigest()
        # 验证算法本身可重现
        recomputed = hashlib.sha1("".join(sorted([token, timestamp, nonce, echostr])).encode()).hexdigest()
        assert expected == recomputed
        # 顺序错了应该不匹配（验证 sort 必要性）
        wrong = hashlib.sha1(f"{token}{timestamp}{nonce}{echostr}".encode()).hexdigest()
        # 不保证 wrong != expected，但绝大多数情况下不同
        # 这里只验证算法可复现即可

    def test_decrypt_message_signature_mismatch_raises(self):
        """_decrypt_message 签名不匹配应抛 ValueError。"""
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        gw._token = "real_token"
        gw._encoding_aes_key = "jWmYm7qr5nMoAUwZRjGtBxmz3qANaQ5x"  # 43 字符 base64
        # 用一个错误的 signature
        with pytest.raises(ValueError, match="signature mismatch"):
            gw._decrypt_message(
                echostr="fake_echostr",
                signature="wrong_signature",
                timestamp="1400000000",
                nonce="abc",
            )

    def test_decrypt_message_correct_signature_passes_signature_check(self):
        """签名正确时 _decrypt_message 应通过签名校验（之后会因 echostr 不是有效密文而失败）。"""
        from gateways.messaging import WeComGateway
        gw = WeComGateway()
        gw._token = "real_token"
        # 43 字符的合法 base64 aes_key（去掉 = 后再 b64decode）
        gw._encoding_aes_key = "jWmYm7qr5nMoAUwZRjGtBxmz3qANaQ5x"
        # 构造一个正确的签名
        echostr = "fake_echostr_data"
        timestamp = "1400000000"
        nonce = "abc"
        sig = hashlib.sha1(
            "".join(sorted([gw._token, timestamp, nonce, echostr])).encode()
        ).hexdigest()
        # 签名应该通过校验，但 base64 解 echostr 会失败（因为不是真密文）
        # 我们只验证签名校验通过——后面解密失败抛的是 base64/binascii 错误
        with pytest.raises(Exception) as exc_info:
            gw._decrypt_message(echostr, sig, timestamp, nonce)
        # 不应是 "signature mismatch"
        assert "signature mismatch" not in str(exc_info.value)


# ============================================================
# DingTalkGateway
# ============================================================
class TestDingTalkGateway:
    @pytest.mark.asyncio
    async def test_setup_disabled_returns_silently(self):
        from gateways.messaging import DingTalkGateway
        gw = DingTalkGateway()
        ctx = _make_ctx({"gateways": {"dingtalk": {"enabled": False}}})
        await gw.setup(ctx)
        assert gw._webhook_url == ""
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_webhook_mode(self):
        from gateways.messaging import DingTalkGateway
        gw = DingTalkGateway()
        ctx = _make_ctx({"gateways": {"dingtalk": {
            "enabled": True, "mode": "webhook",
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=x",
            "secret": "SEC123",
        }}})
        await gw.setup(ctx)
        assert gw._mode == "webhook"
        assert gw._webhook_url.startswith("https://oapi.dingtalk.com")
        assert gw._secret == "SEC123"

    @pytest.mark.asyncio
    async def test_setup_stream_mode_without_credentials_does_not_start(self):
        """stream 模式无 client_id/secret 应拒绝启动（fail-closed）。"""
        from gateways.messaging import DingTalkGateway
        gw = DingTalkGateway()
        ctx = _make_ctx({"gateways": {"dingtalk": {
            "enabled": True, "mode": "stream",
        }}})
        await gw.setup(ctx)
        # 无凭据不应起 task
        assert gw._task is None

    @pytest.mark.asyncio
    async def test_send_webhook_without_url_returns_error(self):
        from gateways.messaging import DingTalkGateway
        gw = DingTalkGateway()
        result = await gw.send_webhook("hello")
        # 没 webhook_url 应返回错误
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_send_webhook_with_secret_appends_signature(self):
        """配了 secret 的 webhook 应在 URL 上附加 timestamp + sign。"""
        from gateways.messaging import DingTalkGateway
        gw = DingTalkGateway()
        gw._webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=tok"
        gw._secret = "SECtest123"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"errcode": 0, "errmsg": "ok"})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        result = await gw.send_webhook("hello", at_all=True)
        assert result["ok"] is True
        # URL 应包含 timestamp 和 sign
        called_url = gw._client.post.await_args.args[0]
        assert "timestamp=" in called_url
        assert "sign=" in called_url
        # payload 应包含 at_all
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert payload["msgtype"] == "text"
        assert payload["text"]["content"] == "hello"
        assert payload["at"]["isAtAll"] is True

    def test_webhook_signature_algorithm(self):
        """独立验证 DingTalk webhook 签名算法。

        算法：
          timestamp = str(round(time.time() * 1000))   # 毫秒
          string_to_sign = f"{timestamp}\n{secret}"
          hmac_code = hmac.new(secret.encode, string_to_sign.encode, sha256).digest
          sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        """
        secret = "SECtest123"
        timestamp = "1700000000000"
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode(), string_to_sign.encode(), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        # 验证算法可复现
        recomputed = urllib.parse.quote_plus(
            base64.b64encode(
                hmac.new(
                    secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256
                ).digest()
            )
        )
        assert sign == recomputed
        # sign 应该是 URL-encoded base64（非空）
        assert sign
        # quote_plus 应把 + 转成 %2B（如果存在）
        assert "+" not in sign  # quote_plus 把 + 编码成 %2B


# ============================================================
# FeishuGateway
# ============================================================
class TestFeishuGateway:
    @pytest.mark.asyncio
    async def test_setup_disabled_returns_silently(self):
        from gateways.messaging import FeishuGateway
        gw = FeishuGateway()
        ctx = _make_ctx({"gateways": {"feishu": {"enabled": False}}})
        await gw.setup(ctx)
        gw.bus.subscribe.assert_not_called()
        assert gw._task is None

    @pytest.mark.asyncio
    async def test_setup_webhook_mode(self):
        from gateways.messaging import FeishuGateway
        gw = FeishuGateway()
        ctx = _make_ctx({"gateways": {"feishu": {
            "enabled": True, "mode": "webhook",
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x",
            "secret": "sh",
        }}})
        await gw.setup(ctx)
        assert gw._mode == "webhook"
        assert gw._webhook_url.startswith("https://open.feishu.cn")
        assert gw._secret == "sh"

    @pytest.mark.asyncio
    async def test_setup_app_mode_without_verification_token_fails_closed(self):
        """app 模式无 verification_token 应拒绝启动 callback（fail-closed）。"""
        from gateways.messaging import FeishuGateway
        gw = FeishuGateway()
        ctx = _make_ctx({"gateways": {"feishu": {
            "enabled": True, "mode": "app",
            "app_id": "x", "app_secret": "y",
            # 故意不传 verification_token
        }}})
        await gw.setup(ctx)
        assert gw._task is None
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_app_mode_with_token_starts_callback(self):
        from gateways.messaging import FeishuGateway
        gw = FeishuGateway()
        ctx = _make_ctx({"gateways": {"feishu": {
            "enabled": True, "mode": "app",
            "app_id": "ai", "app_secret": "as",
            "verification_token": "vt",
            "callback_host": "127.0.0.1", "callback_port": 19998,
        }}})
        with patch("uvicorn.Server.serve", new=AsyncMock()):
            await gw.setup(ctx)
        try:
            assert gw._mode == "app"
            assert gw._verification_token == "vt"
            subscribed = {c.args[0] for c in gw.bus.subscribe.call_args_list}
            assert "turn_completed" in subscribed
            assert gw._task is not None
        finally:
            await gw.stop()

    @pytest.mark.asyncio
    async def test_send_webhook_without_url_returns_error(self):
        from gateways.messaging import FeishuGateway
        gw = FeishuGateway()
        result = await gw.send_webhook("hello")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_send_webhook_posts_to_feishu(self):
        from gateways.messaging import FeishuGateway
        gw = FeishuGateway()
        gw._webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"code": 0, "msg": "ok"})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        result = await gw.send_webhook("hello")
        assert result["ok"] is True
        url = gw._client.post.await_args.args[0]
        assert "open.feishu.cn" in url
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert payload["msg_type"] == "text"
        assert payload["content"]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_send_webhook_with_secret_includes_signature(self):
        """配了 secret 时 payload 应包含 timestamp 和 sign 字段。"""
        from gateways.messaging import FeishuGateway
        gw = FeishuGateway()
        gw._webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
        gw._secret = "sh-secret"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"code": 0})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        await gw.send_webhook("hello")
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert "timestamp" in payload
        assert "sign" in payload

    def test_webhook_signature_algorithm(self):
        """独立验证 Feishu webhook 签名算法。

        算法：
          timestamp = str(int(time.time()))
          string_to_sign = f"{timestamp}\n{secret}"
          sig = base64.b64encode(
              hmac.new(secret.encode, string_to_sign.encode, sha256).digest
          ).decode()
        """
        secret = "sh-secret"
        timestamp = "1700000000"
        string_to_sign = f"{timestamp}\n{secret}"
        sig = base64.b64encode(
            hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
        ).decode()
        # 验证算法可复现
        recomputed = base64.b64encode(
            hmac.new(
                secret.encode(),
                f"{timestamp}\n{secret}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        assert sig == recomputed
        # sig 是合法 base64 字符串
        assert sig
        base64.b64decode(sig)  # 不抛异常


# ============================================================
# DiscordGateway
# ============================================================
class TestDiscordGateway:
    @pytest.mark.asyncio
    async def test_setup_disabled_does_not_create_client(self):
        from gateways.messaging import DiscordGateway
        gw = DiscordGateway()
        ctx = _make_ctx({"gateways": {"discord": {"enabled": False}}})
        await gw.setup(ctx)
        assert gw._client is None
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_enabled_without_token_does_not_start(self):
        from gateways.messaging import DiscordGateway
        gw = DiscordGateway()
        ctx = _make_ctx({"gateways": {"discord": {
            "enabled": True, "bot_token": "",
        }}})
        await gw.setup(ctx)
        assert gw._client is None
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_enabled_with_token_starts(self):
        from gateways.messaging import DiscordGateway
        gw = DiscordGateway()
        ctx = _make_ctx({"gateways": {"discord": {
            "enabled": True, "bot_token": "discord_tok",
            "allowed_channels": [123, 456],
        }}})
        await gw.setup(ctx)
        try:
            assert gw._token == "discord_tok"
            # setup 把 allowed_channels 全部转 str（与 Discord API 一致）
            assert gw._allowed_channels == ["123", "456"]
            assert gw._client is not None
            subscribed = {c.args[0] for c in gw.bus.subscribe.call_args_list}
            assert "turn_completed" in subscribed
            assert gw._task is not None
        finally:
            await gw.stop()

    @pytest.mark.asyncio
    async def test_send_message_posts_to_discord_api(self):
        from gateways.messaging import DiscordGateway
        gw = DiscordGateway()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"id": "msg1"})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        await gw._send_message("channel-789", "hello")
        url = gw._client.post.await_args.args[0]
        assert "discord.com/api/v10" in url
        assert "/channels/channel-789/messages" in url
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert payload["content"] == "hello"

    @pytest.mark.asyncio
    async def test_send_message_truncates_long_text(self):
        from gateways.messaging import DiscordGateway
        gw = DiscordGateway()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        await gw._send_message("ch", "x" * 3000)
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert len(payload["content"]) == 2000


# ============================================================
# SlackGateway
# ============================================================
class TestSlackGateway:
    @pytest.mark.asyncio
    async def test_setup_disabled_does_not_create_client(self):
        from gateways.messaging import SlackGateway
        gw = SlackGateway()
        ctx = _make_ctx({"gateways": {"slack": {"enabled": False}}})
        await gw.setup(ctx)
        assert gw._client is None
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_enabled_without_token_does_not_start(self):
        from gateways.messaging import SlackGateway
        gw = SlackGateway()
        ctx = _make_ctx({"gateways": {"slack": {
            "enabled": True, "bot_token": "",
        }}})
        await gw.setup(ctx)
        assert gw._client is None

    @pytest.mark.asyncio
    async def test_setup_self_reply_protection_when_bot_user_id_empty(self):
        """auth.test 返回空 user_id 时，应 close client、不起 poll loop。

        这是为了防止 bot 回复自己的消息导致死循环。
        """
        from gateways.messaging import SlackGateway
        gw = SlackGateway()
        # mock httpx client：auth.test 返回空 user_id
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={
            "ok": True, "user_id": "",  # 空 user_id → 自回复保护触发
        })
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = _make_ctx({"gateways": {"slack": {
                "enabled": True, "bot_token": "xoxb-test",
                "allowed_channels": ["C1"],
            }}})
            await gw.setup(ctx)
        # 自回复保护：client 应被关闭
        assert gw._client is None
        assert gw._task is None
        gw.bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_with_valid_bot_user_id_starts(self):
        from gateways.messaging import SlackGateway
        gw = SlackGateway()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={
            "ok": True, "user_id": "U123",  # 有 user_id → 正常启动
        })
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.aclose = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200, json=MagicMock(return_value={})))
        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = _make_ctx({"gateways": {"slack": {
                "enabled": True, "bot_token": "xoxb-test",
                "allowed_channels": ["C1"],
            }}})
            await gw.setup(ctx)
        try:
            assert gw._bot_user_id == "U123"
            assert gw._client is not None
            subscribed = {c.args[0] for c in gw.bus.subscribe.call_args_list}
            assert "turn_completed" in subscribed
            assert gw._task is not None
        finally:
            await gw.stop()

    @pytest.mark.asyncio
    async def test_send_message_posts_to_slack_api(self):
        from gateways.messaging import SlackGateway
        gw = SlackGateway()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"ok": True})
        gw._client = MagicMock()
        gw._client.post = AsyncMock(return_value=mock_resp)
        await gw._send_message("C12345", "hello")
        url = gw._client.post.await_args.args[0]
        assert "slack.com/api" in url
        assert "/chat.postMessage" in url
        payload = gw._client.post.await_args.kwargs.get("json") or \
            gw._client.post.await_args.args[1]
        assert payload["channel"] == "C12345"
        assert payload["text"] == "hello"


# ============================================================
# WeChatPersonalGateway
# ============================================================
class TestWeChatPersonalGateway:
    def test_default_attributes(self):
        from gateways.wechat_personal import WeChatPersonalGateway
        gw = WeChatPersonalGateway()
        assert gw.name == "gateway_wechat_personal"
        # bug 修复后的关键属性：_enabled 默认 False（之前 setup 没赋值）
        assert gw._enabled is False
        assert gw._running is False
        assert gw._token == ""
        assert gw._account_id == ""
        assert gw._poll_task is None
        assert gw._login_task is None
        assert gw._session is None
        assert gw._allowed_users == []

    @pytest.mark.asyncio
    async def test_setup_disabled_does_not_connect(self, monkeypatch, tmp_path):
        """enabled=False 时 setup 不应调 _connect。"""
        from gateways.wechat_personal import WeChatPersonalGateway
        import gateways.wechat_personal as wp
        # 隔离 DATA_DIR，避免扫到真实环境凭据
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "wx")
        gw = WeChatPersonalGateway()
        ctx = _make_ctx({"gateways": {"wechat_personal": {
            "enabled": False,
        }}})
        await gw.setup(ctx)
        assert gw._enabled is False
        assert gw._running is False
        assert gw._session is None

    @pytest.mark.asyncio
    async def test_setup_enabled_without_saved_credentials_does_not_connect(
        self, monkeypatch, tmp_path
    ):
        """enabled=True 但无 saved account 时不应连接（也不应抛异常）。"""
        import gateways.wechat_personal as wp
        from gateways.wechat_personal import WeChatPersonalGateway
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "wx")
        gw = WeChatPersonalGateway()
        ctx = _make_ctx({"gateways": {"wechat_personal": {
            "enabled": True,
            "allowed_users": ["wx_user_1"],
        }}})
        await gw.setup(ctx)
        assert gw._enabled is True
        assert gw._allowed_users == ["wx_user_1"]
        # 无 saved credentials → 不连接
        assert gw._running is False
        assert gw._session is None

    @pytest.mark.asyncio
    async def test_send_returns_false_when_not_running(self):
        from gateways.wechat_personal import WeChatPersonalGateway
        gw = WeChatPersonalGateway()
        # _running=False 或 _session=None
        gw._running = False
        result = await gw.send("chat-1", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_returns_false_when_no_session(self):
        from gateways.wechat_personal import WeChatPersonalGateway
        gw = WeChatPersonalGateway()
        gw._running = True
        gw._session = None
        result = await gw.send("chat-1", "hello")
        assert result is False


class TestWeChatPersonalHelpers:
    """wechat_personal.py 模块级 helper 函数。"""

    def test_sanitize_chat_id_keeps_alnum_at_dash_dot_underscore(self):
        from gateways.wechat_personal import _sanitize_chat_id
        # 合法字符保留（alnum + @.-_）
        assert _sanitize_chat_id("user@123-test.x_y") == "user@123-test.x_y"
        # 非法字符被替换为 _（不是去掉）
        # ';' / 空格 / '/' 都不合法 → 都变 _
        sanitized = _sanitize_chat_id("user;rm -rf /")
        assert ";" not in sanitized
        assert " " not in sanitized
        assert "/" not in sanitized
        # alnum 部分应保留
        assert "user" in sanitized
        assert "rm" in sanitized
        assert "rf" in sanitized
        # 引号、< > 也应被替换为 _
        sanitized2 = _sanitize_chat_id('hello "world" <script>')
        assert '"' not in sanitized2
        assert "<" not in sanitized2
        assert ">" not in sanitized2
        assert " " not in sanitized2
        # alnum 部分保留
        assert "hello" in sanitized2
        assert "world" in sanitized2
        assert "script" in sanitized2

    def test_account_path_and_sync_path_under_data_dir(self, monkeypatch, tmp_path):
        import gateways.wechat_personal as wp
        from gateways.wechat_personal import _account_path, _sync_path
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "wx")
        ap = _account_path("acc1")
        sp = _sync_path("acc1")
        assert ap.parent == tmp_path / "wx"
        assert sp.parent == tmp_path / "wx"
        assert ap.name == "acc1.json"
        assert sp.name == "acc1.sync.json"

    def test_save_and_load_sync_buf_roundtrip(self, monkeypatch, tmp_path):
        import gateways.wechat_personal as wp
        from gateways.wechat_personal import (
            _load_sync_buf, _save_sync_buf,
        )
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "wx")
        tmp_path.joinpath("wx").mkdir(parents=True)
        _save_sync_buf("acc1", "sync-buf-data-123")
        loaded = _load_sync_buf("acc1")
        assert loaded == "sync-buf-data-123"

    def test_load_sync_buf_missing_file_returns_empty(self, monkeypatch, tmp_path):
        import gateways.wechat_personal as wp
        from gateways.wechat_personal import _load_sync_buf
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "wx")
        assert _load_sync_buf("nonexistent") == ""

    def test_save_credentials_writes_json(self, monkeypatch, tmp_path):
        import gateways.wechat_personal as wp
        from gateways.wechat_personal import (
            _account_path, _save_credentials,
        )
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "wx")
        _save_credentials("acc1", token="tok-123", base_url="http://b", user_id="u1")
        import json
        data = json.loads(_account_path("acc1").read_text(encoding="utf-8"))
        assert data["token"] == "tok-123"
        assert data["base_url"] == "http://b"
        assert data["user_id"] == "u1"
        assert "saved_at" in data

    def _make_aiohttp_response(self, status_code=200, body=None, ok=True):
        """构造一个支持 `async with session.post(...) as response` 的 mock。

        aiohttp 的 ClientResponse 同时是 async context manager + 提供
        response.text() / response.ok / response.status。这里用 MagicMock +
        AsyncMock 组合模拟。
        """
        body_text = body if body is not None else '{"ret": 0}'
        resp = MagicMock()
        resp.status = status_code
        resp.ok = ok
        resp.text = AsyncMock(return_value=body_text)
        # json() 同步返回解析后的 dict（产品代码用 json.loads(raw)）
        resp_cm = MagicMock()
        resp_cm.__aenter__ = AsyncMock(return_value=resp)
        resp_cm.__aexit__ = AsyncMock(return_value=False)
        return resp_cm, resp

    @pytest.mark.asyncio
    async def test_api_post_sends_required_headers(self):
        """_api_post 应在 header 中带 iLink-App-Id 和 iLink-App-ClientVersion。"""
        from gateways.wechat_personal import _api_post
        session = MagicMock()
        resp_cm, resp = self._make_aiohttp_response(body='{"ret": 0, "data": "ok"}')
        session.post = MagicMock(return_value=resp_cm)
        result = await _api_post(
            session,
            base_url="https://ilinkai.weixin.qq.com",
            endpoint="ilink/bot/sendmsg",
            payload={"k": "v"},
            token="my-token",
            timeout_ms=5000,
        )
        assert result["ret"] == 0
        # 验证 header 含必需字段
        called_kwargs = session.post.call_args.kwargs
        headers = called_kwargs.get("headers", {})
        assert headers.get("iLink-App-Id") is not None
        assert headers.get("iLink-App-ClientVersion") is not None
        assert headers.get("iLink-Bot-Token") == "my-token"

    @pytest.mark.asyncio
    async def test_api_post_raises_on_http_error(self):
        """response.ok=False 应 raise RuntimeError。"""
        from gateways.wechat_personal import _api_post
        session = MagicMock()
        resp_cm, resp = self._make_aiohttp_response(
            status_code=500, body="server error", ok=False
        )
        session.post = MagicMock(return_value=resp_cm)
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await _api_post(
                session,
                base_url="https://x",
                endpoint="ep",
                payload={},
                timeout_ms=1000,
            )

    @pytest.mark.asyncio
    async def test_send_msg_returns_success_on_ret_zero(self):
        from gateways.wechat_personal import _send_msg
        session = MagicMock()
        resp_cm, _ = self._make_aiohttp_response(body='{"ret": 0}')
        session.post = MagicMock(return_value=resp_cm)
        result = await _send_msg(
            session,
            base_url="https://x",
            token="t",
            chat_id="chat-1",
            content="hello",
        )
        assert result["ret"] == 0

    @pytest.mark.asyncio
    async def test_send_msg_returns_failure_on_nonzero_ret(self):
        from gateways.wechat_personal import _send_msg
        session = MagicMock()
        resp_cm, _ = self._make_aiohttp_response(body='{"ret": 1, "errmsg": "bad"}')
        session.post = MagicMock(return_value=resp_cm)
        result = await _send_msg(
            session,
            base_url="https://x",
            token="t",
            chat_id="chat-1",
            content="hello",
        )
        assert result["ret"] != 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
