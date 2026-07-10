"""WeChat login skill - 按需启动微信网关。

Usage: 输入 "微信登录" 或 "登录微信" 即可启动微信网关并显示二维码
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _find_wechat_gateway(ctx_ref=None) -> Optional[Any]:
    """在 ctx._plugins 中查找微信网关实例。"""
    if ctx_ref is not None:
        plugins = getattr(ctx_ref, '_plugins', []) or []
        for plugin in plugins:
            if plugin and getattr(plugin, 'name', '') == 'gateway_wechat_personal':
                return plugin

    try:
        import one_agent as oa  # type: ignore
        app = getattr(oa, '_app_instance', None)
        if app is not None and getattr(app, 'ctx', None) is not None:
            plugins = getattr(app.ctx, '_plugins', []) or []
            for plugin in plugins:
                if plugin and getattr(plugin, 'name', '') == 'gateway_wechat_personal':
                    return plugin
    except Exception as exc:
        logger.debug("ignored non-critical error: %s", exc)

    return None


def _get_ctx(ctx_ref=None) -> Optional[Any]:
    """获取应用上下文。"""
    if ctx_ref is not None:
        return ctx_ref
    try:
        import one_agent as oa  # type: ignore
        app = getattr(oa, '_app_instance', None)
        if app is not None:
            return getattr(app, 'ctx', None)
    except Exception as exc:
        logger.debug("ignored non-critical error: %s", exc)
    return None


async def _ensure_wechat_gateway(ctx_ref=None) -> tuple:
    """确保微信网关可用。如果未加载，动态加载并设置。

    Returns:
        (gateway_instance, error_message)
        如果成功，error_message 为 None
        如果失败，gateway_instance 为 None，error_message 包含原因
    """
    existing = _find_wechat_gateway(ctx_ref)
    if existing is not None:
        return existing, None

    # 动态加载网关插件
    try:
        from gateways.wechat_personal import WeChatPersonalGateway
    except ImportError as e:
        return None, f"无法加载微信网关模块: {e}"

    # 检查依赖
    try:
        import aiohttp  # noqa: F401
        from cryptography.hazmat.backends import default_backend  # noqa: F401
    except ImportError:
        return None, """❌ 缺少微信网关依赖

请先安装依赖：
```bash
pip install aiohttp cryptography
```
安装后重启 One-Agent，再输入 "微信登录"
"""

    ctx = _get_ctx(ctx_ref)
    if ctx is None:
        return None, "无法获取应用上下文，无法启动微信网关"

    # 实例化并 setup
    gateway = WeChatPersonalGateway()
    try:
        await gateway.setup(ctx)
    except Exception as e:
        logger.error(f"微信网关 setup 失败: {e}", exc_info=True)
        return None, f"微信网关初始化失败: {e}"

    # 注册到 ctx._plugins
    if hasattr(ctx, '_plugins'):
        plugins_list = list(ctx._plugins or [])
        plugins_list.append(gateway)
        ctx._plugins = plugins_list

    return gateway, None


def _generate_ascii_qrcode(data: str) -> str:
    """生成 ASCII 二维码，用于在终端直接显示。

    如果 qrcode 库未安装，返回空字符串（调用方回退到 URL 方式）。
    """
    try:
        import io
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)

        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        return buf.getvalue().rstrip()
    except ImportError:
        return ""
    except Exception as e:
        logger.debug(f"ASCII QR code generation failed: {e}")
        return ""


def make_wechat_login_handler(ctx_ref=None):
    """Create the WeChat login skill handler.

    Args:
        ctx_ref: Optional AgentContext reference (passed in by SkillManager
            so the handler can locate the wechat gateway plugin without
            importing non-existent module-level names).
    """

    async def handler(args: Dict[str, Any]) -> str:
        """Handle WeChat login request."""
        # 确保网关已加载（未启用则动态启用）
        gateway, error = await _ensure_wechat_gateway(ctx_ref)
        if error is not None:
            return error
        if gateway is None:
            return "❌ 无法启动微信网关"

        try:
            result = await gateway.login()
            if result:
                qr_url = getattr(gateway, 'qr_url', '')
                running = getattr(gateway, 'running', False)
                if running:
                    return "✅ 微信网关已在运行中\n\n你可以直接在微信上与我对话了"
                elif qr_url:
                    # 尝试在终端直接显示 ASCII 二维码，避免浏览器加载失败
                    ascii_qr = _generate_ascii_qrcode(qr_url)
                    if ascii_qr:
                        return f"""✅ 微信网关启动中，请用微信扫描下方二维码登录：

{ascii_qr}

💡 提示：
- 二维码有效期约 2 分钟，请尽快扫描
- 如二维码过期或显示不清，再次输入 /微信 获取新二维码
- 扫码后在手机上点击确认登录
- 登录成功后即可在微信上与我对话
- 登录凭证会自动保存，下次启动无需重新扫码

如终端二维码无法扫描，也可在手机浏览器打开：
{qr_url}
"""
                    else:
                        return f"""✅ 微信网关启动中，请在手机浏览器打开以下链接用微信扫码登录：

{qr_url}

💡 提示：
- 二维码有效期约 2 分钟，请尽快扫描
- 如二维码过期或打不开，再次输入 /微信 获取新二维码
- 扫码后在手机上点击确认登录
- 登录成功后即可在微信上与我对话
- 登录凭证会自动保存，下次启动无需重新扫码

（提示：安装 qrcode 库可在终端直接显示二维码：pip install qrcode）
"""
                else:
                    return "✅ 微信网关启动中，请稍候...\n（二维码链接将在日志中显示，如长时间无响应请重新输入 /微信）"
            else:
                return "⚠️ 微信网关启动失败，请查看日志"
        except Exception as e:
            logger.error(f"WeChat login error: {e}", exc_info=True)
            return f"❌ 登录失败: {e}"

    return handler


# Skill definition
WECHAT_LOGIN_SKILL = {
    "id": "wechat_login",
    "title": "微信登录",
    "description": "启动微信网关并显示登录二维码，按需启动模式",
    "schema": {
        "type": "object",
        "properties": {}
    },
    "handler_maker": make_wechat_login_handler,
}
