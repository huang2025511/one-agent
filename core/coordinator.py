"""The "coordinator" — wires router → LLM → skills/executors → reply.

This plugin is the single owner of the per-turn execution loop.  It
subscribes to ``turn_routed`` events, calls the LLM with the model +
messages picked by the router, optionally dispatches tool calls, and
finally publishes ``turn_completed`` so gateways can display the reply.

Keeping this separate from both the router and the LLM provider means we
can swap either without touching the control flow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from core.context import TurnContext
from core.events import Event
from core.plugin import Plugin
from core.tool_result import ToolResult
from models import LLMProvider
from skills import SkillManager

logger = logging.getLogger(__name__)

# Coordinator configuration constants
MAX_TOOL_ITERATIONS = 5
DEFAULT_MAX_TOKENS = 2048
MAX_SKILL_FAILURES = 3
TURN_COMPLETION_TIMEOUT = 120.0
# Complexity tier thresholds (determine execution strategy)
EXPERT_COMPLEXITY_THRESHOLD = 0.8   # >= → multi-agent pattern
COMPLEX_COMPLEXITY_THRESHOLD = 0.5  # >= → think + reflect


class Coordinator(Plugin):
    """Runs the per-turn conversation loop."""

    name = "coordinator"
    depends_on = ["llm", "router", "skills"]

    def __init__(self) -> None:
        super().__init__()
        self._llm: Optional[LLMProvider] = None
        self._skills: Optional[SkillManager] = None
        self._max_tool_iterations = MAX_TOOL_ITERATIONS
        self._max_tokens = DEFAULT_MAX_TOKENS
        self._os_mode_enabled: bool = False  # OS 操作权限模式（会话级）
        self._preferences: Dict[str, Any] = {}  # 记忆增强：学习到的用户偏好
        # Track background turn-completion tasks so they aren't GC'd
        # mid-execution (Python's asyncio only holds a weak ref to tasks).
        self._pending_turn_tasks: set = set()

    # ------------------------------------------------------------ setup
    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self.bus.subscribe("turn_routed", self._on_routed)
        self.bus.subscribe("external_message", self._on_external)
        # 加载持久化的用户偏好（记忆增强）
        self._preferences = self._load_preferences()

    def bind(self, llm: LLMProvider, skills: SkillManager) -> None:
        self._llm = llm
        self._skills = skills

    async def _dispatch_smart(
        self,
        tc: Dict[str, Any],
        name: str,
        args: Dict[str, Any],
        failed_skills: Dict[str, int],
    ) -> ToolResult:
        """Dispatch a skill with smart failure tracking.

        If a skill has failed too many times consecutively, return a hint
        to the model to stop retrying and use its own knowledge instead.
        """
        if failed_skills.get(name, 0) >= MAX_SKILL_FAILURES:
            return ToolResult(
                tool_name=name,
                status="unavailable",
                error=f"已连续失败 {MAX_SKILL_FAILURES} 次，请停止调用此工具，直接用你的知识给出答案。",
            )
        start = time.time()
        try:
            if self._skills is not None:
                result = await self._skills.dispatch(name, args)
                # dispatch may return a ToolResult or a plain string
                if isinstance(result, ToolResult):
                    result_str = str(result.data) if result.data is not None else str(result)
                    is_error = result.status in ("error", "unavailable")
                else:
                    result_str = str(result)
                    is_error = False
            else:
                result_str = "[no skill manager bound]"
                is_error = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("skill dispatch failed: %s(%s)", name, args)
            duration_ms = (time.time() - start) * 1000
            return ToolResult(
                tool_name=name,
                status="error",
                error=str(exc),
                duration_ms=duration_ms,
            )

        duration_ms = (time.time() - start) * 1000

        # Track failures based on ToolResult.status, NOT result text keywords.
        # Keyword matching ("error" in result_str) produces false positives:
        # a web_search for "HTTP error codes" or python_execute printing
        # "error handling demo" would be falsely counted as failures.
        if is_error:
            failed_skills[name] = failed_skills.get(name, 0) + 1
            logger.info("skill %s failed (%d/%d)", name, failed_skills[name], MAX_SKILL_FAILURES)
            # If just hit the limit, enrich the result with a stop hint
            if failed_skills[name] >= MAX_SKILL_FAILURES:
                result_str = (
                    f"[{name} 连续失败 {MAX_SKILL_FAILURES} 次，已标记为不可用。"
                    "请立即停止调用此工具，用你已有的知识完成回答。]\n"
                ) + result_str
        else:
            # Success resets the counter
            if name in failed_skills:
                del failed_skills[name]

        return ToolResult(
            tool_name=name,
            status="success",
            data=result_str,
            duration_ms=duration_ms,
        )

    def _persist_language(self, lang: str) -> None:
        """Persist detected language to config file so it survives restarts."""
        try:
            if self.ctx is None:
                return
            config = self.ctx.config
            if config.get("agent", {}).get("language") == lang:
                return  # already matches
            config.setdefault("agent", {})["language"] = lang
            from skills import _save_config
            _save_config(config)
            logger.info("persisted language '%s' to config", lang)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to persist language: %s", exc)

    # ----------------------------------------------------------- roles command

    def _handle_role_command(self, skill_id: str, args_text: str) -> str:
        """处理 /role、/role-list、/role-off 命令。"""
        from core.roles import role_library
        library = getattr(self.ctx, "role_library", None) if getattr(self, "ctx", None) else None
        if library is None:
            library = role_library
            if not library.is_loaded:
                library.load()

        text = (args_text or "").strip()

        # /role-off：关闭当前角色
        if skill_id == "role_off":
            library.off()
            return "✅ 已关闭角色，回到默认助手行为。"

        # /role-list：列出所有角色
        if skill_id == "role_list":
            roles = library.all()
            if not roles:
                return "角色库为空。"
            lines = ["📋 可用角色（共 %d 个）：" % len(roles)]
            for r in roles:
                name = str(r.get("name") or "")
                desc = str(r.get("description") or "")
                marker = "（当前）" if library.current() == name else ""
                lines.append("  - %s%s — %s" % (name, marker, desc))
            lines.append("")
            lines.append("用法：")
            lines.append("  /role <角色名或关键词>   切换角色（支持模糊匹配）")
            lines.append("  /role list               同 /role-list")
            lines.append("  /role search <关键词>   搜索含关键词的角色")
            lines.append("  /role off               同 /role-off，关闭角色")
            return "\n".join(lines)

        # /role <...>
        if not text:
            current = library.current()
            if current:
                return "当前角色：%s\n用 /role list 查看所有角色，用 /role off 关闭。" % current
            return "当前未选择任何角色。用 /role list 查看，或用 /role <角色名> 切换。"

        lowered = text.lower()
        # /role list / off / search xxx
        if lowered in ("list", "ls", "all", "列表", "所有", "清单"):
            return self._handle_role_command("role_list", "")

        if lowered in ("off", "关闭", "取消", "none", "off"):
            library.off()
            return "✅ 已关闭角色，回到默认助手行为。"

        # /role search xxx 或 /role <关键词>
        if lowered.startswith("search ") or lowered.startswith("搜索 "):
            keyword = text.split(None, 1)[1].strip()
        else:
            keyword = text

        # 先尝试精确 / 模糊匹配到单个角色
        match = library.find(keyword)
        if match:
            name = match["name"]
            library.select(name)
            desc = match.get("description") or ""
            return "🎭 已切换角色：%s\n  %s\n\n角色提示词已生效，会在下一条消息中影响助手回复。" % (name, desc)

        # 否则，列出名称 / 描述里含关键词的所有角色
        all_roles = library.all()
        hits = []
        key = keyword.lower()
        for r in all_roles:
            name = str(r.get("name") or "")
            desc = str(r.get("description") or "")
            aliases = " ".join(str(a) for a in (r.get("aliases") or []))
            hay = (name + " " + desc + " " + aliases).lower()
            if key and key in hay:
                hits.append((name, desc))
        if hits:
            lines = ["🔍 匹配到 %d 个角色（用 /role <角色名> 切换）：" % len(hits)]
            for name, desc in hits[:20]:
                lines.append("  - %s — %s" % (name, desc))
            return "\n".join(lines)
        return "❌ 未找到与 \"%s\" 匹配的角色。用 /role list 查看所有可用角色。" % keyword

    # ----------------------------------------------------------- memory / preferences

    _PREFERENCES_PATH = "data/memory/preferences.json"

    def _load_preferences(self) -> Dict[str, Any]:
        """从 data/memory/preferences.json 加载偏好；失败时返回空字典。"""
        import json as _json
        import os as _os
        try:
            dir_path = _os.path.dirname(self._PREFERENCES_PATH)
            if dir_path:
                _os.makedirs(dir_path, exist_ok=True)
            if _os.path.exists(self._PREFERENCES_PATH):
                with open(self._PREFERENCES_PATH, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load preferences: %s", exc)
        return {
            "preferences": [],  # ["偏好以自然语言描述", ...]
            "facts": [],        # ["记住的事实", ...]
            "user_name": "",
            "updated_at": None,
        }

    def _save_preferences(self) -> None:
        """把当前偏好持久化到 data/memory/preferences.json。"""
        import json as _json
        import os as _os
        import time as _time
        try:
            dir_path = _os.path.dirname(self._PREFERENCES_PATH)
            if dir_path:
                _os.makedirs(dir_path, exist_ok=True)
            self._preferences["updated_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self._PREFERENCES_PATH, "w", encoding="utf-8") as f:
                _json.dump(self._preferences, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to save preferences: %s", exc)

    def _format_preferences_prompt(self) -> str:
        """把已学习到的偏好格式化成一段可插入 system prompt 的文本。"""
        prefs = self._preferences or {}
        items = []
        if prefs.get("user_name"):
            items.append(f"- 称呼用户：{prefs['user_name']}")
        for p in prefs.get("preferences", []) or []:
            if p and isinstance(p, str):
                items.append(f"- {p}")
        for f in prefs.get("facts", []) or []:
            if f and isinstance(f, str):
                items.append(f"- {f}")
        if not items:
            return ""
        return "【用户长期偏好（请在合适时使用，不要逐条复述）】\n" + "\n".join(items)

    def _learn_user_preferences(self, user_text: str, reply_text: str) -> bool:
        """启发式地从一轮对话中学习潜在的用户偏好。

        为了避免把噪音也当作记忆，只在检测到强信号时才记录：
        - "我叫 X / 我是 X / 称呼我 X" → 记录 user_name
        - "我喜欢 X / 我偏好 X / 我用 X / 我习惯 X / 请用 X"
        - "我不喜欢 / 不要 X"
        - 首字母大写的专有名词 / 明确的数字或 URL

        返回是否更新了偏好。
        """
        import re as _re
        if not user_text or not isinstance(user_text, str):
            return False
        ut = user_text.strip()

        updated = False

        # 姓名检测
        m = _re.search(r"(?:我叫|我是|称呼我|请叫我|my\s+name\s+is|i['’ ]am)\s+([^\s，,。.!！？?]{2,20})", ut, flags=_re.IGNORECASE)
        if m:
            name = m.group(1).strip().rstrip("。.，,！!？?")
            current = self._preferences.get("user_name") or ""
            if current != name:
                self._preferences["user_name"] = name
                updated = True

        # 中文明确偏好句式
        pref_zh_patterns = [
            (r"(我喜欢|我偏好|我常用|我习惯|请用|我希望|我想要|我需要|我应该用|我的\s*编辑器\s*是|我的\s*语言\s*是)\s*[:：]?\s*([^。!！?？\n]{2,60})", "用户"),
            (r"(我不喜欢|不要|别用|请勿|不要用)\s*[:：]?\s*([^。!！?？\n]{2,40})", "用户不喜欢"),
        ]
        prefs = list(self._preferences.setdefault("preferences", []))
        normalized_ut = ut.lower()
        for pattern, prefix in pref_zh_patterns:
            for m in _re.finditer(pattern, ut):
                detail = f"{prefix}{m.group(1).strip()}：{m.group(2).strip()}"
                # 去重：如果已有相似内容就不重复记录
                if not any(p and detail in p for p in prefs):
                    prefs.append(detail)
                    updated = True
        self._preferences["preferences"] = prefs

        # 英文名/工具偏好检测（简单关键词）
        for keyword in ("vscode", "vim", "neovim", "emacs", "sublime", "jetbrains", "github copilot", "linux", "macos", "windows", "python", "typescript", "rust", "go", "docker"):
            if _re.search(rf"\b{keyword}\b", normalized_ut):
                facts = list(self._preferences.setdefault("facts", []))
                fact = f"用户提到并可能使用 {keyword}"
                if fact not in facts and len(facts) < 30:
                    facts.append(fact)
                    updated = True
                self._preferences["facts"] = facts

        if updated:
            self._save_preferences()
            logger.info("preferences updated: %s", self._preferences)
        return updated

    async def _compress_messages(self, messages: list, turn) -> str:
        """Use a lightweight LLM call to summarize early conversation."""
        if not self._llm:
            return ""
        early_text = "\n".join(
            f"{m['role']}: {str(m.get('content', ''))[:500]}"
            for m in messages[:max(1, len(messages) // 2)]
            if m.get("role") in ("user", "assistant") and not m.get("tool_calls")
        )
        if not early_text.strip():
            return ""
        # Use lightweight model if configured, otherwise fall back to turn model
        model = turn.model
        if self.ctx and self.ctx.config:
            lightweight = self.ctx.config.get("llm", {}).get("lightweight_model")
            if lightweight:
                model = lightweight
        prompt = [
            {"role": "system", "content": "你是对话摘要助手。用2-3句话总结以下对话的关键信息、用户需求和已完成的步骤。只输出摘要，不要加任何前缀。"},
            {"role": "user", "content": early_text[:4000]},
        ]
        try:
            resp = await self._llm.chat_completion(
                messages=prompt,
                model=model,
                max_tokens=200,
                tools=None,
            )
            return resp.get("text", "").strip()
        except Exception as exc:
            logger.debug("context compression LLM call failed: %s", exc)
            return ""

    @staticmethod
    def _detect_complex_task(text: str) -> bool:
        """Quick heuristic: tasks with comparison, research, or analysis keywords."""
        keywords = ["比较", "对比", "分析", "研究", "评估", "调查", "分别", "各",
                    "compare", "analyze", "research", "evaluate", "both", "each"]
        return len(text) > 50 and any(k in text for k in keywords)

    # ------------------------------------------------------------ OS 模式处理
    async def _handle_os_mode(
        self, turn: TurnContext, cmd: str, args_text: str,
    ) -> None:
        """Handle /os-on, /os-off, /os-mode commands.

        OS 模式 = 用户授权 One-Agent 可以直接调用 system_run 工具
        （安装软件、执行脚本、apt-get / pip 等），无需每次加 /shell 前缀。
        危险命令（DANGEROUS 级别）仍然需要额外确认。

        /os-on <password>  — 开启 OS 模式（同时验证密码、缓存授权）
        /os-off            — 关闭 OS 模式
        /os-mode           — 查看当前 OS 模式状态
        """
        from i18n import get_language
        lang = (get_language() or "zh").lower()

        # /os-mode — 查询状态
        if cmd == "_os_mode":
            if self._os_mode_enabled:
                status = "已开启" if lang.startswith("zh") else "ENABLED"
                msg = (
                    f"OS 模式: {status}\n"
                    "当前可以自动执行系统命令（pip / npm / apt-get / curl 等）。\n"
                    "使用 /os-off 可关闭。"
                )
            else:
                status = "已关闭" if lang.startswith("zh") else "DISABLED"
                msg = (
                    f"OS 模式: {status}\n"
                    "One-Agent 不能直接执行系统命令，只能通过 /shell 前缀调用。\n"
                    "使用 /os-on <密码> 可开启。"
                )
            turn.result = msg
            return

        # /os-off — 关闭 OS 模式
        if cmd in ("_os_off", "disable-os", "关闭os", "关闭系统权限"):
            self._os_mode_enabled = False
            turn.meta["os_mode"] = False
            # 使 SystemExecutor 的密码缓存失效
            await self._invalidate_os_cache()
            msg = "OS 模式已关闭。One-Agent 不能再直接操作系统命令。" if lang.startswith("zh") else "OS mode DISABLED. One-Agent can no longer directly execute system commands."
            turn.result = msg
            return

        # /os-on — 开启 OS 模式（需要密码验证）
        password = args_text.strip()

        # 如果密码未提供，要求用户输入
        if not password:
            turn.result = (
                "OS 模式开启需要密码验证。\n"
                "用法: /os-on <你的密码>\n\n"
                "示例: /os-on mypassword123\n\n"
                "开启后，One-Agent 可以直接执行系统命令（如 pip install、apt-get、curl 等），\n"
                "无需加 /shell 前缀。危险命令仍需额外确认。"
                if lang.startswith("zh")
                else "Usage: /os-on <your_password>\n\n"
                "After enabling, One-Agent can directly execute system commands "
                "(pip install, apt-get, curl, etc.) without the /shell prefix."
            )
            return

        # 验证密码并开启 OS 模式
        success = await self._enable_os_mode(turn, password)
        if success:
            self._os_mode_enabled = True
            turn.meta["os_mode"] = True
            msg = (
                "✅ OS 模式已开启！\n\n"
                "One-Agent 现在可以直接帮你操作系统：\n"
                "  - pip install / npm install / apt-get install\n"
                "  - curl / wget 下载文件\n"
                "  - 创建目录、移动文件\n"
                "  - 运行自定义脚本\n\n"
                "【重要】危险命令（如 rm -rf、sudo、格式化）仍需你额外确认。\n"
                "使用 /os-off 可关闭此权限。"
                if lang.startswith("zh")
                else "✅ OS mode ENABLED!\n\n"
                "One-Agent can now directly help with system operations:\n"
                "  - pip install / npm install / apt-get install\n"
                "  - curl / wget downloads\n"
                "  - create dirs, move files\n"
                "  - run custom scripts\n\n"
                "DANGEROUS commands (rm -rf, sudo, mkfs...) still require your explicit confirmation.\n"
                "Use /os-off to disable."
            )
        else:
            msg = (
                "❌ OS 模式开启失败：密码错误。\n"
                "请检查密码后重试。连续 3 次错误会锁定 5 分钟。"
                if lang.startswith("zh")
                else "❌ OS mode failed: incorrect password.\n"
                "Please check and retry. 3 wrong attempts = 5-minute lockout."
            )
        turn.result = msg

    async def _enable_os_mode(self, turn: TurnContext, password: str) -> bool:
        """Verify password and enable OS mode for this session."""
        if self._skills is None:
            return False
        try:
            # 通过 system_unlock 技能验证密码（会缓存授权）
            result = await self._skills.dispatch("system_unlock", {"password": password})
            ok = "成功" in str(result) or "success" in str(result).lower() or "✅" in str(result)
            if ok:
                logger.info("OS mode enabled for session %s", turn.session_id)
            return ok
        except Exception as exc:
            logger.warning("OS mode enable failed: %s", exc)
            return False

    async def _invalidate_os_cache(self) -> None:
        """Invalidate the SystemExecutor password cache."""
        if self._skills is None:
            return
        try:
            skill = self._skills.get("system_lock")
            if skill:
                await self._skills.dispatch("system_lock", {})
        except Exception as exc:
            logger.warning("system_lock handler failed: %s", exc)

    # ------------------------------------------------------------ slash commands
    # Mapping from slash command names (both EN and CN) to skill IDs
    _SLASH_COMMANDS: Dict[str, str] = {
        # ---------- 系统信息与帮助 ----------
        "help": "help", "帮助": "help", "帮助信息": "help", "怎么用": "help", "menu": "help", "菜单": "help",
        "status": "status", "状态": "status", "info": "status", "信息": "status", "系统状态": "status",
        "version": "version", "版本": "version", "ver": "version", "about": "version", "关于": "version",
        "skills": "list_skills", "skill": "list_skills", "技能": "list_skills", "技能列表": "list_skills",
        "tools": "list_skills", "工具": "list_skills", "工具列表": "list_skills",
        "history": "history", "历史": "history", "历史记录": "history", "对话历史": "history",
        "clear": "clear", "清屏": "clear", "清空": "clear", "清除": "clear", "cls": "clear",
        # ---------- 配置 ----------
        "settings": "settings", "设置": "settings", "配置": "settings", "设定": "settings",
        "config": "settings", "configure": "settings", "配置项": "settings",
        # ---------- 更新与维护 ----------
        "update": "updater", "更新": "updater", "升级": "updater", "upgrade": "updater",
        "restart": "restart", "重启": "restart", "reboot": "restart",
        # ---------- 网关管理 ----------
        "wechat": "wechat_login", "微信": "wechat_login", "微信登录": "wechat_login",
        "gateways": "list_gateways", "网关": "list_gateways", "网关列表": "list_gateways",
        # ---------- 退出 ----------
        "quit": "quit", "退出": "quit", "关机": "quit", "再见": "quit", "exit": "quit", "bye": "quit",
        # ---------- 计算与时间 ----------
        "calc": "calc", "计算": "calc", "计算器": "calc", "算": "calc", "math": "calc",
        "time": "now", "时间": "now", "当前时间": "now", "现在几点了": "now", "date": "now", "日期": "now",
        # ---------- 笔记 ----------
        "note": "save_note", "笔记": "save_note", "记录": "save_note", "记事": "save_note",
        # ---------- 搜索与网络 ----------
        "search": "web_search", "搜索": "web_search", "网络搜索": "web_search", "google": "web_search",
        "百度": "web_search", "baidu": "web_search",
        # ---------- 多媒体 ----------
        "transcribe": "transcribe", "转文字": "transcribe", "语音转文字": "transcribe", "stt": "transcribe",
        "image": "describe_image", "图片": "describe_image", "图片描述": "describe_image",
        "看图": "describe_image", "vision": "describe_image",
        # ---------- 文档 ----------
        "doc": "document_search", "docs": "document_search", "文档": "document_search",
        "文档搜索": "document_search", "document": "document_search",
        # ---------- 代码执行 ----------
        "py": "python_execute", "python": "python_execute", "代码": "python_execute",
        "执行": "python_execute", "执行python": "python_execute", "run": "python_execute",
        # ---------- 系统操作（Shell/Docker） ----------
        "shell": "system_run", "sh": "system_run", "命令": "system_run", "系统命令": "system_run",
        "exec": "system_run", "execute": "system_run", "运行": "system_run",
        "unlock": "system_unlock", "解锁": "system_unlock", "授权": "system_unlock",
        "lock": "system_lock", "锁定": "system_lock", "撤销授权": "system_lock",
        # ---------- OS 模式（操作系统操作权限） ----------
        "os-on": "_os_on", "os_on": "_os_on", "os-off": "_os_off", "os_off": "_os_off",
        "os-mode": "_os_mode", "osmode": "_os_mode", "os": "_os_mode",
        "enable-os": "_os_on", "disable-os": "_os_off",
        "开启os": "_os_on", "关闭os": "_os_off", "开启系统权限": "_os_on", "关闭系统权限": "_os_off",
        # ---------- 角色（roles） ----------
        "role": "role", "roles": "role", "角色": "role", "切换角色": "role",
        "role-list": "role_list", "角色列表": "role_list", "角色清单": "role_list",
        "role-off": "role_off", "角色关闭": "role_off", "关闭角色": "role_off",
    }

    async def _handle_slash_command(self, turn: TurnContext) -> bool:
        """Handle slash commands like /help, /settings.

        Returns True if the command was handled (no further processing needed),
        False otherwise.
        """
        text = turn.input_text.strip()
        if not text.startswith("/"):
            return False

        # Parse command: /command or /command arg1 arg2 ...
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        args_text = parts[1] if len(parts) > 1 else ""

        # Look up command in mapping (try exact match first, then partial)
        skill_id = None

        # Try exact match
        if cmd in self._SLASH_COMMANDS:
            skill_id = self._SLASH_COMMANDS[cmd]
        else:
            # Try partial match (e.g., "/help me" → "/help")
            for key in self._SLASH_COMMANDS:
                if cmd.startswith(key) or key.startswith(cmd):
                    skill_id = self._SLASH_COMMANDS[key]
                    break

        if skill_id is None:
            turn.result = f"未知命令: /{cmd}。支持的命令: {', '.join(sorted(set(self._SLASH_COMMANDS.keys())))}"
            self.publish("turn_completed", turn=turn)
            return True

        # ---- OS mode commands (handled directly, not via skill dispatch) ----
        if skill_id in ("_os_on", "_os_off", "_os_mode"):
            await self._handle_os_mode(turn, skill_id, args_text)
            self.publish("turn_completed", turn=turn)
            return True

        # ---- 角色系统（roles）：直接处理，避免依赖某个 skill ----
        if skill_id in ("role", "role_list", "role_off"):
            turn.result = self._handle_role_command(skill_id, args_text)
            self.publish("turn_completed", turn=turn)
            return True

        # Dispatch to skill
        if self._skills is None:
            turn.result = "[技能系统未初始化]"
            self.publish("turn_completed", turn=turn)
            return True

        skill = self._skills.get(skill_id)
        if skill is None:
            turn.result = f"[技能不存在: {skill_id}]"
            self.publish("turn_completed", turn=turn)
            return True

        # Build args - for most skills, put remaining text as 'input' arg
        args: Dict[str, Any] = {}
        if args_text:
            if skill_id == "system_run":
                # system_run: args_text is the command to run (password can be embedded with --password)
                # Support: /shell ls -la  or  /shell ls -la --password mypass
                if "--password" in args_text:
                    cmd_part, pwd_part = args_text.split("--password", 1)
                    args["command"] = cmd_part.strip()
                    args["password"] = pwd_part.strip()
                else:
                    args["command"] = args_text
            elif skill_id == "system_unlock":
                # /unlock <password>
                args["password"] = args_text
            else:
                args["input"] = args_text
        elif skill_id == "system_run":
            # /shell without command — show usage
            turn.result = "用法: /shell <命令> [--password <密码>]\n示例:\n  /shell ls -la\n  /shell ls -la --password mypass123\n  /unlock mypass123 (先解锁，60分钟内有效)"
            self.publish("turn_completed", turn=turn)
            return True
        elif skill_id == "system_unlock":
            turn.result = "用法: /unlock <密码>\n解锁后 60 分钟内执行危险命令不需要再次输入密码。"
            self.publish("turn_completed", turn=turn)
            return True

        try:
            result = await self._skills.dispatch(skill_id, args)
            turn.result = str(result)
        except Exception as exc:
            logger.exception("slash command dispatch failed: %s", exc)
            turn.result = f"[执行错误: {exc}]"

        self.publish("turn_completed", turn=turn)
        return True

    # ------------------------------------------------------------ handlers
    async def _on_routed(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or turn.result is not None or turn.error is not None:
            return

        # Handle slash commands first
        if turn.input_text and turn.input_text.strip().startswith("/"):
            if await self._handle_slash_command(turn):
                return

        # Auto-detect language from user input
        if turn.input_text:
            from i18n import detect_language, get_language, set_language
            detected_lang = detect_language(turn.input_text)
            current_lang = get_language()
            if detected_lang != current_lang:
                set_language(detected_lang)
                logger.info("Auto-detected language: %s from user input", detected_lang)
                # Persist language preference to config
                self._persist_language(detected_lang)

        # avoid double-processing — if something already published a reply,
        # skip this turn entirely
        try:
            await self._run_turn(turn)
        except Exception as exc:  # noqa: BLE001
            logger.error("coordinator failed: %s", exc, exc_info=True)
            turn.record_failure(str(exc))
            self.publish("turn_completed", turn=turn)

    async def _on_external(self, event: Event) -> None:
        """Handle messages coming from chat platforms.

        External messages arrive in a loose format; we normalize them into
        a TurnContext so they flow through the same pipeline.

        Note: this handler must return quickly so the event bus can process
        the ``user_message`` we publish here. The wait for ``turn_completed``
        is delegated to a background task.
        """
        text = event.get("text") or ""
        session_id = event.get("session_id") or event.get("chat_id") or "ext"

        # Auto-detect language on first user message
        from i18n import auto_detect_and_switch
        auto_detect_and_switch(text)

        turn = TurnContext(input_text=text, source=event.get("source", "ext"), session_id=str(session_id))
        # publish user_message so the router classifies this — routing
        # publishes turn_routed which eventually reaches _on_routed above.
        self.publish("user_message", turn=turn, session_id=turn.session_id)

        # Wait for completion in a background task so we don't block the bus.
        # Blocking here would deadlock the bus: it can't process user_message
        # (and thus never reach turn_completed) while awaiting this handler.
        # Save a reference to prevent the task from being GC'd mid-execution.
        task = asyncio.create_task(self._await_turn_completion(turn, session_id))
        self._pending_turn_tasks.add(task)
        task.add_done_callback(self._pending_turn_tasks.discard)

    async def _await_turn_completion(self, turn: TurnContext, session_id: str) -> None:
        """Wait for a turn to complete, with timeout.

        On timeout, mark the turn as errored so that if it hasn't started
        yet (still queued behind other events), ``_on_routed`` will skip it.
        If it's already running, the result will be computed but ignored by
        the gateway (which has already unsubscribed).
        """
        completion_event = asyncio.Event()

        def _on_turn_completed(evt: Event) -> None:
            completed_turn = evt.get("turn")
            if completed_turn is turn:
                completion_event.set()

        self.bus.subscribe("turn_completed", _on_turn_completed)
        try:
            await asyncio.wait_for(completion_event.wait(), timeout=TURN_COMPLETION_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Turn completion timeout for session %s", session_id)
            # Mark the turn so _on_routed skips it if it hasn't started yet.
            if turn.result is None and turn.error is None:
                turn.record_failure("turn completion timeout")
        finally:
            self.bus.unsubscribe("turn_completed", _on_turn_completed)

    # --------------------------------------------------------- main loop
    async def _run_turn(self, turn: TurnContext) -> None:
        """Execute a single turn with tiered execution strategy based on complexity.

        Tiered execution strategy (independent from model selection):
        - trivial/simple (< 0.5): direct execution, no thinking/reflection
        - complex (0.5–0.8): think + reflect before executing
        - expert (≥ 0.8): multi-agent pattern (planner + executor)

        This is orthogonal to model tier selection — both work together:
        e.g., an expert task gets both the strongest model AND multi-agent execution.
        """
        if turn is None:
            raise RuntimeError("turn cannot be None")
        if turn.input_text is None:
            raise RuntimeError("turn.input_text cannot be None")

        if self._llm is None:
            turn.record_failure("LLM provider not bound")
            self.publish("turn_completed", turn=turn)
            return

        if turn.model is None:
            raise RuntimeError("Model must be set before execution")

        messages = self._prepare_messages(turn)
        tools = self._prepare_tools(turn)

        # Get complexity from router classification
        complexity = getattr(turn, "estimated_complexity", 0.0)
        logger.debug("turn complexity: %.2f", complexity)

        # Expert level: multi-agent pattern
        if complexity >= EXPERT_COMPLEXITY_THRESHOLD:
            if await self._multi_agent_phase(messages, turn):
                return  # multi-agent handled it, skip normal flow

        # Complex level: think + reflect
        elif complexity >= COMPLEX_COMPLEXITY_THRESHOLD:
            await self._think_phase(messages, turn)
            await self._reflect_phase(messages, turn)

        # Simple/trivial: skip thinking entirely for speed
        else:
            # Still do context compression for long conversations
            await self._compress_context(messages, turn)
            # Direct tool loop without thinking overhead
            await self._tool_loop(messages, turn, tools)
            self._extract_entities(turn)
            self.publish("turn_completed", turn=turn)
            logger.info("reply produced (simple mode, %d tokens, %.2fs)",
                        turn.tokens_used, turn.duration_seconds or 0)
            return

        # Context compression (for complex/expert after thinking phases)
        await self._compress_context(messages, turn)

        # Tool-call loop (for complex level that didn't use multi-agent)
        await self._tool_loop(messages, turn, tools)

        # Auto-extract entities
        self._extract_entities(turn)

        self.publish("turn_completed", turn=turn)
        logger.info("reply produced (complex mode, %d tokens, %.2fs)",
                    turn.tokens_used, turn.duration_seconds or 0)

    def _prepare_messages(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Prepare message list with memory snippets from long-term memory + KG.

        Memory is injected as a dedicated assistant-style "relevant memory" message
        rather than quietly appended to the user message, so the LLM can reliably
        see it. The router is responsible for putting the system prompt + history
        + user message into ``turn.messages``; we layer memory on top here.
        """
        if turn is None:
            raise RuntimeError("turn cannot be None")
        if turn.input_text is None:
            raise RuntimeError("turn.input_text cannot be None")

        messages = list(turn.messages)

        # ——— Active memory retrieval (the core fix) ———
        # MemoryPlugin subscribes to user_message too, but we can't rely on
        # subscription order. Instead, reach directly for ctx.memory and run
        # the search here, so memory_snippets are guaranteed to be present
        # before the LLM call.
        memory_snippets = turn.meta.get("memory_snippets")
        if not memory_snippets and self.ctx is not None:
            memory_plugin = getattr(self.ctx, "memory", None)
            if memory_plugin is not None:
                try:
                    retrieved = self._retrieve_memory_for(turn, memory_plugin)
                    if retrieved:
                        memory_snippets = retrieved
                        turn.meta["memory_snippets"] = retrieved
                except Exception as exc:
                    logger.warning("active memory retrieval failed: %s", exc)

        if memory_snippets:
            from i18n import get_language
            lang = (get_language() or "zh").lower()
            if lang.startswith("zh"):
                mem_header = "【相关记忆】（来自长期记忆/知识图谱/语义检索）\n以下内容是我从之前的对话和知识中记住的，最与当前问题相关的信息。\n如果与问题直接相关，请优先使用，不要重复问或重复查；如果不相关请忽略，不要编造。\n\n"
            else:
                mem_header = "[Relevant Memory] (from long-term memory / knowledge graph / semantic retrieval)\nThe following lines are what I remembered from earlier that best relate to the current question.\nIf directly relevant, USE THEM first; if not, ignore — don't make things up.\n\n"
            memory_block = {"role": "assistant", "content": mem_header + memory_snippets}

            # Insert memory block right BEFORE the last user message, so the LLM
            # sees memory right before reading the user's actual request. If no
            # user message exists at the end, append instead.
            if messages and messages[-1].get("role") == "user":
                messages.insert(len(messages) - 1, memory_block)
            else:
                messages.append(memory_block)
                # Guarantee at least one user message carries the input
                if not any(m.get("role") == "user" for m in messages):
                    messages.append({"role": "user", "content": turn.input_text})

        # ——— 偏好注入（记忆增强） ———
        pref_text = self._format_preferences_prompt()
        if pref_text:
            pref_block = {"role": "system", "content": pref_text}
            # 插入到最后一条 system 消息之后，保持上下文自然
            insert_idx = 0
            for i, m in enumerate(messages):
                if m.get("role") == "system":
                    insert_idx = i + 1
            messages.insert(insert_idx, pref_block)

        return messages

    def _retrieve_memory_for(self, turn: TurnContext, memory_plugin) -> str:
        """Query long-term memory + knowledge graph for relevant snippets."""
        hits: List[str] = []
        query = turn.input_text or ""
        if not query.strip():
            return ""

        # 1) Long-term FTS5 / hybrid search
        long_term = getattr(memory_plugin, "_long", None)
        if long_term is not None:
            try:
                fts_hits = long_term.search(query, limit=5) or []
                for h in fts_hits:
                    content = h.get("content", "")
                    source = h.get("source", "memory")
                    if content and len(content) > 5:
                        hits.append(f"- [记忆/{source}] {content[:300]}")
            except Exception as exc:
                logger.debug("long-term memory search failed: %s", exc)

        # 2) Embedding semantic search
        embeddings = getattr(memory_plugin, "_embeddings", None)
        if embeddings is not None:
            try:
                query_vec = embeddings.embed(query)
                if query_vec is not None:
                    sem = embeddings.search(query_vec, top_k=5) or []
                    seen_contents = {h.split("] ", 1)[1][:40] for h in hits}
                    for memory_id, _score in sem:
                        entry = long_term.get_by_id(memory_id) if long_term else None
                        content = (entry or {}).get("content", "") if isinstance(entry, dict) else str(entry or "")
                        if content and content[:40] not in seen_contents:
                            seen_contents.add(content[:40])
                            hits.append(f"- [语义记忆] {content[:300]}")
            except Exception as exc:
                logger.debug("embedding memory search failed: %s", exc)

        # 3) Knowledge Graph — entities related to keywords in query
        kg = getattr(memory_plugin, "_kg", None)
        if kg is not None:
            try:
                kg_hits = kg.search(query, limit=5) or []
                for h in kg_hits:
                    if isinstance(h, dict):
                        content = h.get("content", h.get("label", ""))
                    else:
                        content = str(h)
                    if content:
                        hits.append(f"- [知识图谱] {content[:300]}")
            except Exception as exc:
                logger.debug("KG memory search failed: %s", exc)

        if not hits:
            return ""

        from i18n import get_language
        lang = (get_language() or "zh").lower()
        if lang.startswith("zh"):
            header = "以下是从我的记忆系统中检索到的、与当前问题最相关的内容 — 请优先参考：\n"
        else:
            header = "Retrieved from memory — most relevant to current question:\n"
        return header + "\n".join(hits[:5])

    def _prepare_tools(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Pick relevant skills and prepare tool schemas.

        When OS mode is enabled (via /os-on), system_run is automatically added
        to the tool list so the LLM can directly call it for system operations.
        """
        if turn is None:
            raise RuntimeError("turn cannot be None")

        tools: List[Dict[str, Any]] = []
        if self._skills is not None:
            chosen = self._skills.pick_relevant(turn.input_text, limit=4)
            web_search = self._skills.get("web_search")
            if web_search and web_search not in chosen:
                chosen.insert(0, web_search)
            # OS mode: auto-add system_run so the LLM can call it directly
            if self._os_mode_enabled:
                system_run = self._skills.get("system_run")
                if system_run and system_run not in chosen:
                    chosen.append(system_run)
            turn.skills = [s.id for s in chosen]
            tools = [s.schema for s in chosen]
        else:
            turn.skills = []
        return tools

    async def _think_phase(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Execute structured thinking phase (Chain-of-Thought style planning).

        This is the thinking backbone of One-Agent. Instead of the previous
        "think 2-4 sentences then act", we ask the LLM to produce a real
        7-step plan. The plan is appended to ``messages`` as a structured
        assistant response, so every subsequent tool-loop call can see the
        plan and is more likely to follow it instead of drifting into
        superficial chatter.

        Steps we guide the model to produce:
        1. Intent + output form
        2. Known facts / memory hits / prior context
        3. Information gaps (must lookup vs. can infer)
        4. Breakdown into 3-5 concrete sub-steps
        5. Tool assignment per sub-step with rationale
        6. Failure modes + fallbacks
        7. Envisioned final output shape

        The thinking is NOT shown to the user directly — it drives execution.
        """
        if messages is None:
            raise RuntimeError("messages cannot be None")
        if turn is None:
            raise RuntimeError("turn cannot be None")

        from i18n import get_language
        lang = (get_language() or "zh").lower()

        # Build the thinking prompt.  We attach it as an additional user
        # message so the model has access to the full conversation history
        # (including memory) while planning.
        memory_snippets = turn.meta.get("memory_snippets") or ""
        if lang.startswith("zh"):
            plan_prompt = (
                "【内部思考 — 不要输出给用户，只用于内部规划】\n\n"
                "请按以下 7 步为当前用户的问题做一个结构化规划。每一步都必须写清楚，不能省略。\n\n"
                "Step 1. 真正要什么：用一句话提炼用户的核心意图和期望输出形式（代码/答案/方案/列表/对比等）。\n"
                "Step 2. 我已经知道什么：列出对话上下文、相关记忆、常识里已经有的信息。"
                + (("\n  - 相关记忆摘要：" + memory_snippets[:300]) if memory_snippets else "")
                + "\nStep 3. 还缺什么：明确哪些信息必须外部获取（查/算/跑），哪些可以合理推断。\n"
                "Step 4. 拆解任务：把整个任务拆成 3-5 个可执行的小步骤，每一步用一行描述。\n"
                "Step 5. 工具选择：为每个子步骤指定一个最合适的工具（例如 web_search / calc / now / system_run / 具体 skill），并写一句为什么选它。\n"
                "Step 6. 风险与兜底：如果某个步骤失败，有什么替代方案？如果所有工具都不可用，最后怎么给用户一个有用的答案？\n"
                "Step 7. 预期输出：用 1-2 句话描述最终结果应该长什么样（例如『一个对比表格』、『可运行的 Python 代码』、『分 4 点的行动建议』）。\n\n"
                "重要：不要输出最终答案给用户。只输出上述 7 步的规划内容，作为你自己的执行计划。"
            )
        else:
            plan_prompt = (
                "[Internal thinking — DO NOT show this to the user, planning only]\n\n"
                "Please produce a structured plan in 7 steps. Do not skip any step.\n\n"
                "Step 1. What does the user actually want? one sentence capturing intent + output form (code / answer / plan / list / comparison, etc.).\n"
                "Step 2. What do I already know? list context from conversation, relevant memory, and common sense."
                + (("\n  - Memory summary: " + memory_snippets[:300]) if memory_snippets else "")
                + "\nStep 3. What am I still missing? clearly separate what must be looked up from what can be reasonably inferred.\n"
                "Step 4. Break it down: cut the task into 3-5 concrete, executable sub-steps — one line each.\n"
                "Step 5. Pick tools: assign the BEST tool per sub-step (e.g. web_search / calc / now / system_run / a specific skill) and one-sentence rationale.\n"
                "Step 6. Risks and fallbacks: if a step fails, what is plan B? If every tool fails, what useful answer can I still give?\n"
                "Step 7. Envision final output: 1-2 sentences describing what the result should look like (e.g. 'a comparison table', 'runnable Python code', '4-point action plan').\n\n"
                "Important: do NOT write the final user answer. Produce only these 7 steps as your own execution plan."
            )

        thinking_messages = list(messages) + [{"role": "user", "content": plan_prompt}]

        try:
            think_resp = await self._llm.chat_completion(
                messages=thinking_messages,
                model=turn.model,
                max_tokens=min(turn.token_budget or 2048, 900),
                tools=None,  # planning only, no tools
            )
            thinking_text = (think_resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("think phase skipped: %s", exc)
            thinking_text = ""

        if thinking_text:
            turn.meta["thinking"] = thinking_text
            # Inject the plan as an assistant message so the subsequent tool
            # loop sees it — this is the key behavioral change. We also tag
            # it with a visible header so the LLM understands this is its
            # own plan.
            header = "【我的执行计划】\n" if lang.startswith("zh") else "[My execution plan]\n"
            plan_message = {"role": "assistant", "content": header + thinking_text}
            messages.append(plan_message)
            # Append a short user follow-up so conversation flow is preserved
            # and the model knows it should now execute the plan.
            prompt = (
                "好。现在按照上面的计划一步一步执行。"
                if lang.startswith("zh")
                else "Good. Now execute the plan step by step."
            )
            messages.append({"role": "user", "content": prompt})
            logger.debug("think phase completed (%d chars)", len(thinking_text))
        else:
            turn.meta["thinking"] = ""

    async def _reflect_phase(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Execute reflection phase — critically review the plan before execution.

        For complex tasks (complexity >= 0.5), after creating the initial plan,
        we ask the model to reflect on potential flaws and improvements. This
        meta-cognition step helps catch errors before they lead to wasted
        execution.

        The reflection is injected into the message flow as an assistant message,
        so the subsequent tool loop can benefit from the improved plan.
        """
        if messages is None:
            raise RuntimeError("messages cannot be None")
        if turn is None:
            raise RuntimeError("turn cannot be None")

        from i18n import get_language
        lang = (get_language() or "zh").lower()

        plan_text = turn.meta.get("thinking", "")
        if not plan_text:
            logger.debug("reflect phase skipped: no thinking available")
            return

        if lang.startswith("zh"):
            reflect_prompt = (
                "【内部反思 — 不要输出给用户】\n\n"
                "你刚刚制定了一个执行计划。现在请站在更高的角度审视这个计划，找出潜在问题和改进空间。\n\n"
                "请思考并回答以下问题：\n"
                "1. 计划中最大的风险是什么？哪个环节最可能失败？\n"
                "2. 是否遗漏了用户可能关心的边界情况或细节？\n"
                "3. 各个步骤之间是否存在依赖关系没考虑到？\n"
                "4. 如果某个工具调用失败，备用方案是否足够有效？\n"
                "5. 是否有更高效的路径可以达到相同目标？\n"
                "6. 最终输出是否真的能满足用户的核心需求？\n\n"
                "请用简洁的语言总结你的反思结论，并给出具体的改进建议（如果有的话）。"
            )
        else:
            reflect_prompt = (
                "[Internal reflection — DO NOT show this to the user]\n\n"
                "You've created an execution plan. Now step back and critically review it for potential flaws.\n\n"
                "Please answer these questions:\n"
                "1. What is the biggest risk in this plan? Which step is most likely to fail?\n"
                "2. Are there any edge cases or details the user might care about that were missed?\n"
                "3. Are there dependencies between steps that weren't considered?\n"
                "4. If a tool call fails, is the fallback sufficient?\n"
                "5. Is there a more efficient path to the same goal?\n"
                "6. Will the final output truly address the user's core need?\n\n"
                "Summarize your reflections and provide specific improvement suggestions if any."
            )

        reflect_messages = list(messages) + [{"role": "user", "content": reflect_prompt}]

        try:
            reflect_resp = await self._llm.chat_completion(
                messages=reflect_messages,
                model=turn.model,
                max_tokens=min(turn.token_budget or 2048, 500),
                tools=None,
            )
            reflect_text = (reflect_resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("reflect phase skipped: %s", exc)
            reflect_text = ""

        if reflect_text:
            turn.meta["reflection"] = reflect_text
            header = "【我的反思与改进】\n" if lang.startswith("zh") else "[My reflection and improvements]\n"
            reflect_message = {"role": "assistant", "content": header + reflect_text}
            messages.append(reflect_message)
            # Append a user prompt to acknowledge reflection and continue
            prompt = (
                "好。根据你的反思，如果需要调整计划，请立即执行调整后的方案。"
                if lang.startswith("zh")
                else "Good. Based on your reflection, execute with any adjustments needed."
            )
            messages.append({"role": "user", "content": prompt})
            logger.debug("reflect phase completed (%d chars)", len(reflect_text))
        else:
            turn.meta["reflection"] = ""

    async def _multi_agent_phase(self, messages: List[Dict[str, Any]], turn: TurnContext) -> bool:
        """Execute multi-agent pattern for expert-level tasks (complexity >= 0.8).

        This implements a Planner-Executor pattern:
        1. Planner agent: deep analysis of the problem, breaking it into sub-tasks
        2. Executor agent: executes each sub-task sequentially

        Returns True if delegation was successful (no need for further processing),
        False otherwise (fall back to normal flow).
        """
        if messages is None:
            raise RuntimeError("messages cannot be None")
        if turn is None:
            raise RuntimeError("turn cannot be None")

        from i18n import get_language
        _lang = (get_language() or "zh").lower()

        try:
            from core.sub_agent import DelegationManager
            delegator = DelegationManager(self._llm, self._skills)
            result = await delegator.execute(turn.input_text, turn.model)

            if result.get("parallel"):
                turn.result = result["result"]
                turn.meta["delegation_used"] = True
                turn.meta["subtask_count"] = len(result["subtasks"])
                turn.meta["delegation_total_tokens"] = result["total_tokens"]
                turn.record_success(result["result"], result.get("total_tokens", 0))

                if self.ctx and hasattr(self.ctx, 'memory') and hasattr(self.ctx.memory, '_kg') and self.ctx.memory._kg:
                    full_text = f"{turn.input_text}\n{result['result']}"
                    try:
                        count = self.ctx.memory._kg.extract_from_text(full_text, source=turn.session_id)
                        if count > 0:
                            logger.debug("Extracted %d entities from multi-agent turn %s", count, turn.session_id)
                    except Exception as exc:
                        logger.debug("KG extraction failed in multi-agent: %s", exc)

                self.publish("turn_completed", turn=turn)
                logger.info("multi-agent completed (%d subtasks, %d tokens)",
                            result.get("subtask_count", 0),
                            result.get("total_tokens", 0))
                return True

        except Exception as exc:
            logger.warning("multi-agent failed, falling back to normal flow: %s", exc)

        return False

    async def _compress_context(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Compress context if approaching token limit."""
        if messages is None:
            raise RuntimeError("messages cannot be None")
        if turn is None:
            raise RuntimeError("turn cannot be None")

        if not (self.ctx and self.ctx.config):
            return

        compression_enabled = self.ctx.config.get("router", {}).get("context_compression", {}).get("enabled", True)
        if not compression_enabled:
            return

        max_tokens = self.ctx.config.get("memory", {}).get("short_term", {}).get("max_tokens", 8000)
        estimated_tokens = sum(len(str(m.get("content", ""))) // 4 for m in messages)

        if estimated_tokens <= max_tokens * 0.8:
            return

        summary = await self._compress_messages(messages, turn)
        if summary:
            keep_recent = max(4, len(messages) // 3)
            early = messages[:len(messages) - keep_recent]
            recent = messages[len(messages) - keep_recent:]
            messages.clear()
            messages.append({"role": "system", "content": f"[对话历史摘要]\n{summary}"})
            messages.extend(recent)
            turn.meta["context_compressed"] = True
            turn.meta["compressed_messages"] = len(early)

    async def _try_delegation(self, turn: TurnContext, messages: List[Dict[str, Any]]) -> bool:
        """Try delegation for complex tasks. Returns True if delegation was used."""
        if turn is None:
            raise RuntimeError("turn cannot be None")
        if messages is None:
            raise RuntimeError("messages cannot be None")

        if not (turn.meta.get("enable_delegation") or self._detect_complex_task(turn.input_text)):
            return False

        try:
            from core.sub_agent import DelegationManager
            delegator = DelegationManager(self._llm, self._skills)
            result = await delegator.execute(turn.input_text, turn.model)

            if result.get("parallel"):
                turn.result = result["result"]
                turn.meta["delegation_used"] = True
                turn.meta["subtask_count"] = len(result["subtasks"])
                turn.meta["delegation_total_tokens"] = result["total_tokens"]
                turn.record_success(result["result"], result.get("total_tokens", 0))

                # Auto-extract entities
                if self.ctx and hasattr(self.ctx, 'memory') and hasattr(self.ctx.memory, '_kg') and self.ctx.memory._kg:
                    full_text = f"{turn.input_text}\n{result['result']}"
                    try:
                        count = self.ctx.memory._kg.extract_from_text(full_text, source=turn.session_id)
                        if count > 0:
                            logger.debug("Extracted %d entities from turn %s", count, turn.session_id)
                    except Exception as exc:
                        logger.debug("KG extraction failed: %s", exc)

                self.publish("turn_completed", turn=turn)
                logger.info("delegation completed (%d subtasks, %d tokens, %.2fs)",
                            result.get("subtask_count", 0),
                            result.get("total_tokens", 0),
                            result.get("duration_ms", 0) / 1000)
                return True
        except Exception as exc:
            logger.warning("delegation failed, falling back to normal flow: %s", exc)

        return False

    async def _tool_loop(self, messages: List[Dict[str, Any]], turn: TurnContext, tools: List[Dict[str, Any]]) -> None:
        """Execute tool-call loop until final reply."""
        if messages is None:
            raise RuntimeError("messages cannot be None")
        if turn is None:
            raise RuntimeError("turn cannot be None")
        if tools is None:
            raise RuntimeError("tools cannot be None")

        final_text = ""
        total_tokens = 0
        _failed_skills: Dict[str, int] = {}

        for i in range(self._max_tool_iterations):
            resp = await self._llm.chat_completion(
                messages=messages,
                model=turn.model,
                max_tokens=turn.token_budget if i == 0 else self._max_tokens,
                tools=tools or None,
            )
            total_tokens += int(resp.get("tokens_used") or 0)
            tool_calls = resp.get("tool_calls") or []

            # 工具降级方案：如果 provider 不支持结构化 tool_calls，
            # 尝试从助手输出文本中解析 <|tool_call|>...<|/tool_call|> 或
            # ```json {"name": ..., "arguments": {...}}``` 形式的工具调用。
            if not tool_calls:
                text = resp.get("text", "") or ""
                parsed = self._parse_text_tool_calls(text)
                if parsed:
                    tool_calls = parsed
                    # 移除文本中被解析过的工具调用标记，把剩余文本作为
                    # assistant 消息保留，以免工具循环无限重复。
                    cleaned = self._strip_tool_call_markers(text)
                    if cleaned.strip():
                        messages.append({"role": "assistant", "content": cleaned})
                    elif tool_calls:
                        # 没有剩余文本，但有工具调用：保留一条 assistant 消息
                        # 以维持对话交替结构；真实 tool_calls 在执行时追加。
                        messages.append({
                            "role": "assistant",
                            "content": "",
                            "tool_calls_raw": tool_calls,
                        })
                else:
                    final_text = text
                    if final_text:
                        messages.append({"role": "assistant", "content": final_text})
                    break
            else:
                # 正常结构化 tool_calls 路径：保留 text 到 messages
                text = resp.get("text", "") or ""
                if text:
                    messages.append({"role": "assistant", "content": text})

            if tool_calls:
                await self._execute_tool_calls(messages, turn, tool_calls, _failed_skills, i)

            # Check if all skills failed
            if i >= 1 and all(
                name in _failed_skills and _failed_skills[name] >= MAX_SKILL_FAILURES
                for name in [tc.get("name", "") for tc in tool_calls]
            ):
                messages.append({
                    "role": "user",
                    "content": (
                        "[系统提示：你刚才调用的工具都暂时不可用。"
                        "请根据你已经知道的知识直接给出最佳答案，不要再尝试调用工具。]"
                    ),
                })

        else:
            # Loop exhausted
            await self._handle_loop_exhaustion(messages, turn)
            resp = await self._llm.chat_completion(
                messages=messages, model=turn.model, max_tokens=self._max_tokens,
            )
            final_text = resp.get("text", "") or "(no reply)"
            total_tokens += int(resp.get("tokens_used") or 0)

        # Record failure for self-improvement
        if turn.result is None and turn.error:
            self._record_self_improvement(turn)

        if not final_text:
            final_text = "(no reply produced)"
        turn.record_success(final_text, total_tokens)

        # 记忆增强：在每轮对话成功后，检查用户消息中是否有值得记住的内容
        try:
            self._learn_user_preferences(turn.input_text or "", final_text or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("preference learning skipped: %s", exc)

    def _parse_text_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        """从非结构化助手输出中解析工具调用指令。

        当 provider 不支持结构化 tool_calls 时，用户的 system prompt 会
        要求 LLM 用下列文本格式之一输出工具调用：

        1) <|tool_call|>{"name": "skill_id", "arguments": {...}}<|/tool_call|>
        2) ```json
              {"name": "skill_id", "arguments": {...}}
           ```
        3) <tool name="skill_id">{"key": "value"}</tool>

        返回与 provider tool_calls 相同结构的 dict 列表：
            [{"id": "...", "name": "...", "arguments": {...}}, ...]
        """
        import json as _json
        import re as _re
        if not text:
            return []

        calls: List[Dict[str, Any]] = []

        # 1) <|tool_call|>...<|/tool_call|>
        for m in _re.finditer(r"<\|tool_call\|>([\s\S]*?)<\|/tool_call\|>", text):
            try:
                obj = _json.loads(m.group(1).strip())
                name = str(obj.get("name") or obj.get("tool") or "")
                args = obj.get("arguments") or obj.get("params") or obj.get("args") or {}
                if name:
                    calls.append({
                        "id": f"txt_{len(calls)}",
                        "name": name,
                        "arguments": args if isinstance(args, dict) else {"value": args},
                    })
            except _json.JSONDecodeError:
                continue

        # 2) ```json ... ```
        if not calls:
            for m in _re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text):
                try:
                    obj = _json.loads(m.group(1).strip())
                    if isinstance(obj, dict):
                        objs = [obj]
                    elif isinstance(obj, list):
                        objs = obj
                    else:
                        continue
                    for item in objs:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name") or item.get("tool") or "")
                        args = item.get("arguments") or item.get("params") or item.get("args") or {}
                        if name:
                            calls.append({
                                "id": f"txt_{len(calls)}",
                                "name": name,
                                "arguments": args if isinstance(args, dict) else {"value": args},
                            })
                except _json.JSONDecodeError:
                    continue

        # 3) <tool name="...">...</tool>
        if not calls:
            for m in _re.finditer(r'<tool\s+name="([^"]+)"[^>]*>([\s\S]*?)</tool>', text):
                try:
                    name = m.group(1).strip()
                    body = m.group(2).strip()
                    try:
                        args = _json.loads(body)
                        if not isinstance(args, dict):
                            args = {"value": args}
                    except _json.JSONDecodeError:
                        args = {"content": body}
                    if name:
                        calls.append({
                            "id": f"txt_{len(calls)}",
                            "name": name,
                            "arguments": args,
                        })
                except Exception:
                    continue

        return calls

    def _strip_tool_call_markers(self, text: str) -> str:
        """移除文本中的工具调用标记，避免把它们再塞回 LLM 上下文重复执行。"""
        import re as _re
        if not text:
            return text
        text = _re.sub(r"<\|tool_call\|>[\s\S]*?<\|/tool_call\|>", "", text)
        text = _re.sub(r"```(?:json)?\s*[\s\S]*?```", "", text)
        text = _re.sub(r'<tool\s+name="[^"]+"[^>]*>[\s\S]*?</tool>', "", text)
        return text.strip()

    async def _execute_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        turn: TurnContext,
        tool_calls: List[Dict[str, Any]],
        failed_skills: Dict[str, int],
        iteration: int,
    ) -> None:
        """Execute tool calls and append results to messages."""
        if messages is None:
            raise RuntimeError("messages cannot be None")
        if turn is None:
            raise RuntimeError("turn cannot be None")
        if tool_calls is None:
            raise RuntimeError("tool_calls cannot be None")
        if failed_skills is None:
            raise RuntimeError("failed_skills cannot be None")
        if iteration < 0:
            raise RuntimeError("iteration must be non-negative")

        provider = turn.model.split("/")[0] if turn.model and "/" in turn.model else "openai"

        if provider == "anthropic":
            for idx, tc in enumerate(tool_calls):
                name = tc.get("name") or ""
                args = tc.get("args") or {}
                result = await self._dispatch_smart(tc, name, args, failed_skills)

                if result.status == "unavailable" and self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver:
                    self.ctx.self_improver.record_failure(
                        user_input=turn.input_text,
                        error_type="tool_unavailable",
                        error_detail=f"Tool {name} unavailable",
                    )

                turn.meta.setdefault("tool_results", []).append(result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_{idx}",
                    "content": result.to_message(),
                })
        else:
            raw_tool_calls = turn.meta.get("tool_calls_raw") or tool_calls
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": raw_tool_calls,
            })
            for tc in tool_calls:
                name = tc.get("name") or ""
                args = tc.get("args") or {}
                result = await self._dispatch_smart(tc, name, args, failed_skills)

                if result.status == "unavailable" and self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver:
                    self.ctx.self_improver.record_failure(
                        user_input=turn.input_text,
                        error_type="tool_unavailable",
                        error_detail=f"Tool {name} unavailable",
                    )

                turn.meta.setdefault("tool_results", []).append(result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "name": name,
                    "content": result.to_message(),
                })

    async def _handle_loop_exhaustion(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Handle when tool loop reaches max iterations."""
        assert messages is not None, "messages cannot be None"
        assert turn is not None, "turn cannot be None"

        messages.append({
            "role": "user",
            "content": (
                "[系统提示：工具调用已达上限。请根据你已有的知识和前面获取的信息，"
                "直接给用户一个完整、有用的最终答复。不要提及工具不可用或搜索失败。]"
            ),
        })

        # Apply self-improvement suggestions
        if self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver:
            patterns = self.ctx.self_improver.analyze_patterns()
            if patterns:
                for p in patterns[:2]:
                    suggestion = p.get("suggestion", "")
                    if suggestion:
                        turn.meta["improvement_suggestion"] = suggestion
                        inject_msg = {"role": "system", "content": f"[自改进提示] {suggestion}"}
                        if inject_msg not in messages:
                            messages.insert(-2, inject_msg)

    def _record_self_improvement(self, turn: TurnContext) -> None:
        """Record failure for self-improvement analysis."""
        assert turn is not None, "turn cannot be None"

        if not (self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver):
            return

        error_type = turn.error if isinstance(turn.error, str) else turn.error.get("type", "unknown") if isinstance(turn.error, dict) else "unknown"
        error_detail = turn.error if isinstance(turn.error, str) else turn.error.get("detail", str(turn.error)) if isinstance(turn.error, dict) else str(turn.error)

        self.ctx.self_improver.record_failure(
            user_input=turn.input_text,
            error_type=error_type,
            error_detail=error_detail,
            turn_meta=turn.meta,
        )

    def _extract_entities(self, turn: TurnContext) -> None:
        """Auto-extract entities from turn for knowledge graph.

        Only extract from successful turns with a non-empty result —
        extracting from error turns or empty results would pollute the
        KG with user questions or placeholder text like "(no reply produced)".
        """
        assert turn is not None, "turn cannot be None"

        if not (self.ctx and hasattr(self.ctx, 'memory') and hasattr(self.ctx.memory, '_kg') and self.ctx.memory._kg):
            return

        # Skip error turns and turns with no meaningful result
        if turn.error is not None:
            return
        if not turn.result or not turn.result.strip():
            return

        full_text = f"{turn.input_text}\n{turn.result}"

        try:
            count = self.ctx.memory._kg.extract_from_text(full_text, source=turn.session_id)
            if count > 0:
                logger.debug("Extracted %d entities from turn %s", count, turn.session_id)
        except Exception as exc:
            logger.debug("KG extraction failed: %s", exc)
