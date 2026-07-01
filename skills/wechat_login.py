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
    except Exception:
        pass

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
    except Exception:
        pass
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
        import itchat  # type: ignore  # noqa: F401
    except ImportError:
        return None, """❌ 缺少 itchat-uos 依赖

请先安装微信网关依赖：
```bash
pip install itchat-uos
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

        # 如果网关被禁用了（setup 时 self._enabled=False），手动启用
        if not getattr(gateway, '_enabled', False):
            try:
                gateway._enabled = True
                # 从配置中读取其他参数
                cfg = {}
                try:
                    gateways_cfg = getattr(gateway.ctx, 'config', {}) if gateway.ctx else {}
                    if isinstance(gateways_cfg, dict):
                        wxp = (gateways_cfg.get('gateways') or {}).get('wechat_personal') or {}
                    else:
                        wxp = (getattr(gateways_cfg, 'gateways', None) or {}).get('wechat_personal') or {}
                    cfg = wxp or {}
                except Exception:
                    pass
                gateway._allowed_users = [str(u).strip() for u in (cfg.get('allowed_users') or []) if str(u).strip()]
                gateway._reply_prefix = str(cfg.get('reply_prefix', ''))
                gateway._hot_reload = bool(cfg.get('hot_reload', False))
                # 订阅事件总线
                if gateway.bus is not None:
                    gateway.bus.subscribe("turn_completed", gateway._on_done)
            except Exception as e:
                logger.error(f"手动启用微信网关失败: {e}")
                return f"❌ 启用微信网关失败: {e}"

        try:
            result = await gateway.login()
            if result:
                return """✅ 微信网关启动中...

请在终端中扫描显示的二维码完成登录

💡 提示：
- 二维码有效期有限，请尽快扫描
- 扫码后即可在微信上与我对话
- 如需退出，输入 "微信退出"
"""
            else:
                return "⚠️ 微信网关已在运行中"
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
