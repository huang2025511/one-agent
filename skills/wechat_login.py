"""WeChat login skill - 按需启动微信网关。

Usage: 输入 "微信登录" 或 "登录微信" 即可启动微信网关并显示二维码
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def make_wechat_login_handler():
    """Create the WeChat login skill handler."""

    async def handler(args: Dict[str, Any]) -> str:
        """Handle WeChat login request."""
        from skills import _skills

        # 尝试获取微信网关实例
        wechat_gateway = None

        # 方法1: 从 ctx 获取
        try:
            from skills import _ctx_ref
            if _ctx_ref:
                plugins = getattr(_ctx_ref, '_plugins', []) or []
                for plugin in plugins:
                    if plugin and getattr(plugin, 'name', '') == 'gateway_wechat_personal':
                        wechat_gateway = plugin
                        break
        except Exception:
            pass

        # 方法2: 从全局变量获取
        if wechat_gateway is None:
            try:
                # 尝试从全局变量获取
                import sys
                for name, obj in sys.modules.items():
                    if 'wechat' in name.lower():
                        pass
            except Exception:
                pass

        # 直接尝试获取网关
        if wechat_gateway is None:
            try:
                # 从 one_agent 获取
                import one_agent as oa
                if hasattr(oa, '_ctx'):
                    plugins = getattr(oa._ctx, '_plugins', []) or []
                    for plugin in plugins:
                        if plugin and getattr(plugin, 'name', '') == 'gateway_wechat_personal':
                            wechat_gateway = plugin
                            break
            except Exception:
                pass

        if wechat_gateway is None:
            return """❌ 微信网关未启用

请先在配置文件中启用微信网关：

```yaml
# config/default_config.yaml
gateways:
  wechat_personal:
    enabled: true
```

启用后重启 One-Agent，然后再次输入 "微信登录"
"""

        try:
            # 调用登录方法
            result = await wechat_gateway.login()
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
            logger.error(f"WeChat login error: {e}")
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
