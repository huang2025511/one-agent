"""用户体验增强模块 — 提供快捷命令、快捷键和主题切换功能。

提供：
  - 快捷命令系统
  - 快捷键绑定
  - 主题切换
  - 响应式布局支持
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class Shortcut:
    """快捷键定义类。"""
    key: str
    description: str
    action: str
    modifier: Optional[str] = None


@dataclass
class Command:
    """快捷命令定义类。"""
    name: str
    pattern: str
    description: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Theme:
    """主题定义类。"""
    name: str
    display_name: str
    primary_color: str
    secondary_color: str
    background_color: str
    text_color: str
    accent_color: str


class ShortcutManager:
    """快捷键管理器 — 管理键盘快捷键绑定。"""

    DEFAULT_SHORTCUTS = [
        Shortcut("Ctrl+S", "保存当前内容", "save"),
        Shortcut("Ctrl+Z", "撤销操作", "undo"),
        Shortcut("Ctrl+Y", "重做操作", "redo"),
        Shortcut("Ctrl+F", "搜索", "search"),
        Shortcut("Ctrl+N", "新建会话", "new_session"),
        Shortcut("Ctrl+W", "关闭会话", "close_session"),
        Shortcut("Ctrl+Tab", "切换会话", "switch_session"),
        Shortcut("Ctrl+R", "刷新", "refresh"),
        Shortcut("Esc", "取消/关闭弹窗", "cancel"),
        Shortcut("Enter", "确认", "confirm"),
    ]

    def __init__(self):
        self._shortcuts: Dict[str, Shortcut] = {}
        self._handlers: Dict[str, Callable] = {}
        self._load_defaults()

    def _load_defaults(self):
        """加载默认快捷键。"""
        for shortcut in self.DEFAULT_SHORTCUTS:
            self._shortcuts[shortcut.key] = shortcut

    def register_shortcut(self, key: str, description: str, action: str):
        """注册快捷键。"""
        self._shortcuts[key] = Shortcut(key, description, action)

    def unregister_shortcut(self, key: str):
        """取消注册快捷键。"""
        if key in self._shortcuts:
            del self._shortcuts[key]

    def get_shortcut(self, key: str) -> Optional[Shortcut]:
        """获取快捷键定义。"""
        return self._shortcuts.get(key)

    def list_shortcuts(self) -> List[Shortcut]:
        """获取所有快捷键列表。"""
        return list(self._shortcuts.values())

    def register_handler(self, action: str, handler: Callable):
        """注册动作处理器。"""
        self._handlers[action] = handler

    def handle_shortcut(self, key: str) -> bool:
        """处理快捷键触发。"""
        shortcut = self._shortcuts.get(key)
        if shortcut and shortcut.action in self._handlers:
            try:
                self._handlers[shortcut.action]()
                return True
            except Exception as exc:
                logger.error("Failed to handle shortcut %s: %s", key, exc)
        return False


class CommandManager:
    """命令管理器 — 管理快捷命令解析和执行。"""

    DEFAULT_COMMANDS = [
        Command("clear", "^/clear$", "清空当前会话", "clear_session"),
        Command("help", "^/help$", "显示帮助信息", "show_help"),
        Command("version", "^/version$", "显示版本信息", "show_version"),
        Command("status", "^/status$", "显示系统状态", "show_status"),
        Command("config", "^/config(?:\\s+(\\w+))?$", "显示/修改配置", "config", {"key": None}),
        Command("role", "^/role(?:\\s+(\\w+))?$", "切换角色", "switch_role", {"role": None}),
        Command("exit", "^/exit$", "退出", "exit"),
        Command("restart", "^/restart$", "重启服务", "restart"),
        Command("backup", "^/backup$", "创建备份", "create_backup"),
        Command("restore", "^/restore(?:\\s+(\\d+))?$", "恢复备份", "restore_backup", {"index": None}),
        Command("export", "^/export(?:\\s+(\\w+))?$", "导出数据", "export_data", {"type": None}),
        Command("scan", "^/scan$", "扫描漏洞", "scan_vulnerabilities"),
        Command("log", "^/log(?:\\s+(\\w+))?$", "查看日志", "view_log", {"level": None}),
        Command("memory", "^/memory(?:\\s+(\\w+))?$", "管理记忆", "manage_memory", {"action": None}),
        Command("task", "^/task(?:\\s+(\\w+))?$", "管理任务", "manage_task", {"action": None}),
        Command("workflow", "^/workflow(?:\\s+(\\w+))?$", "管理工作流", "manage_workflow", {"action": None}),
    ]

    def __init__(self):
        self._commands: List[Command] = []
        self._handlers: Dict[str, Callable] = {}
        self._load_defaults()

    def _load_defaults(self):
        """加载默认命令。"""
        for cmd in self.DEFAULT_COMMANDS:
            self._commands.append(cmd)

    def register_command(self, name: str, pattern: str, description: str, action: str, params: Dict[str, Any] = None):
        """注册新命令。"""
        self._commands.append(Command(name, pattern, description, action, params or {}))

    def unregister_command(self, name: str):
        """取消注册命令。"""
        self._commands = [c for c in self._commands if c.name != name]

    def parse_command(self, input_text: str) -> Optional[Command]:
        """解析输入文本，匹配命令。"""
        input_text = input_text.strip()
        
        for cmd in self._commands:
            match = re.match(cmd.pattern, input_text)
            if match:
                # 填充捕获的参数
                params = cmd.params.copy()
                groups = match.groups()
                param_keys = [k for k in params.keys() if params[k] is None]
                for i, key in enumerate(param_keys):
                    if i < len(groups):
                        params[key] = groups[i]
                return Command(cmd.name, cmd.pattern, cmd.description, cmd.action, params)
        
        return None

    def register_handler(self, action: str, handler: Callable):
        """注册动作处理器。"""
        self._handlers[action] = handler

    def execute_command(self, cmd: Command) -> Any:
        """执行命令。"""
        if cmd.action in self._handlers:
            try:
                return self._handlers[cmd.action](**cmd.params)
            except Exception as exc:
                logger.error("Failed to execute command %s: %s", cmd.name, exc)
                return None
        return None

    def list_commands(self) -> List[Command]:
        """获取所有命令列表。"""
        return self._commands


class ThemeManager:
    """主题管理器 — 管理界面主题切换。"""

    DEFAULT_THEMES = [
        Theme(
            "light", "明亮",
            primary_color="#4F46E5",
            secondary_color="#7C3AED",
            background_color="#FFFFFF",
            text_color="#1F2937",
            accent_color="#3B82F6"
        ),
        Theme(
            "dark", "深色",
            primary_color="#6366F1",
            secondary_color="#8B5CF6",
            background_color="#1F2937",
            text_color="#F9FAFB",
            accent_color="#60A5FA"
        ),
        Theme(
            "system", "跟随系统",
            primary_color="#4F46E5",
            secondary_color="#7C3AED",
            background_color="system",
            text_color="system",
            accent_color="#3B82F6"
        ),
        Theme(
            "high-contrast", "高对比度",
            primary_color="#0000FF",
            secondary_color="#8B0000",
            background_color="#FFFFFF",
            text_color="#000000",
            accent_color="#FF0000"
        ),
    ]

    def __init__(self):
        self._themes: Dict[str, Theme] = {}
        self._current_theme = "light"
        self._theme_file = Path("data/theme.json")
        self._load_defaults()
        self._load_config()

    def _load_defaults(self):
        """加载默认主题。"""
        for theme in self.DEFAULT_THEMES:
            self._themes[theme.name] = theme

    def _load_config(self):
        """加载主题配置。"""
        if self._theme_file.exists():
            try:
                with open(self._theme_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self._current_theme = config.get("current_theme", "light")
            except Exception:
                pass

    def _save_config(self):
        """保存主题配置。"""
        self._theme_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._theme_file, 'w', encoding='utf-8') as f:
            json.dump({"current_theme": self._current_theme}, f)

    def add_theme(self, theme: Theme):
        """添加自定义主题。"""
        self._themes[theme.name] = theme

    def remove_theme(self, name: str):
        """移除主题。"""
        if name in self._themes and name != "light" and name != "dark":
            del self._themes[name]

    def get_theme(self, name: str) -> Optional[Theme]:
        """获取主题定义。"""
        return self._themes.get(name)

    def list_themes(self) -> List[Theme]:
        """获取所有主题列表。"""
        return list(self._themes.values())

    def set_theme(self, name: str) -> bool:
        """设置当前主题。"""
        if name in self._themes:
            self._current_theme = name
            self._save_config()
            return True
        return False

    def get_current_theme(self) -> Theme:
        """获取当前主题。"""
        return self._themes.get(self._current_theme, self._themes["light"])

    def get_current_theme_name(self) -> str:
        """获取当前主题名称。"""
        return self._current_theme

    def get_theme_css(self) -> str:
        """生成主题CSS样式。"""
        theme = self.get_current_theme()
        return f"""
:root {{
    --primary-color: {theme.primary_color};
    --secondary-color: {theme.secondary_color};
    --background-color: {theme.background_color};
    --text-color: {theme.text_color};
    --accent-color: {theme.accent_color};
}}
"""


class UXPlugin(Plugin):
    """用户体验增强插件。"""

    name = "ux"

    def __init__(self):
        super().__init__()
        self._shortcut_manager = ShortcutManager()
        self._command_manager = CommandManager()
        self._theme_manager = ThemeManager()

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        logger.info("UX plugin configured")

    def get_shortcut_manager(self) -> ShortcutManager:
        """获取快捷键管理器。"""
        return self._shortcut_manager

    def get_command_manager(self) -> CommandManager:
        """获取命令管理器。"""
        return self._command_manager

    def get_theme_manager(self) -> ThemeManager:
        """获取主题管理器。"""
        return self._theme_manager

    def parse_input(self, input_text: str) -> Optional[Command]:
        """解析用户输入，检测命令。"""
        return self._command_manager.parse_command(input_text)

    def execute_action(self, action: str, **kwargs) -> Any:
        """执行动作。"""
        if action in self._command_manager._handlers:
            return self._command_manager.execute_command(
                Command("", "", "", action, kwargs)
            )
        return None

    def register_action(self, action: str, handler: Callable):
        """注册动作处理器（同时注册到命令和快捷键）。"""
        self._command_manager.register_handler(action, handler)
        self._shortcut_manager.register_handler(action, lambda: handler())

    def list_all_actions(self) -> Dict[str, Any]:
        """列出所有可用动作。"""
        return {
            "shortcuts": [s.__dict__ for s in self._shortcut_manager.list_shortcuts()],
            "commands": [c.__dict__ for c in self._command_manager.list_commands()],
            "themes": [t.__dict__ for t in self._theme_manager.list_themes()],
            "current_theme": self._theme_manager.get_current_theme_name()
        }