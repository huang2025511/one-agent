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
SIMPLE_COMPLEXITY_THRESHOLD = 0.2  # >= → light self-verification
# Smart boost feature flags by complexity tier
# - trivial (<0.2): direct execution, no enhancements (max speed)
# - simple (0.2-0.5): light self-verification only
# - complex (0.5-0.8): full pre-thinking + reflection + self-verification + final polish + clarification
# - expert (>=0.8): everything + post-execution review + tool chain planning
SELF_VERIFY_MIN_COMPLEXITY = 0.2     # simple and above
CLARIFICATION_MIN_COMPLEXITY = 0.5      # complex and above
FINAL_POLISH_MIN_COMPLEXITY = 0.5     # complex and above
POST_REFLECT_MIN_COMPLEXITY = 0.8       # expert only
TOOL_CHAIN_PLANNING_MIN_COMPLEXITY = 0.8  # expert only


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
        # Track background turn-completion tasks so they aren't GC'd
        # mid-execution (Python's asyncio only holds a weak ref to tasks).
        self._pending_turn_tasks: set = set()

    # ------------------------------------------------------------ setup
    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self.bus.subscribe("turn_routed", self._on_routed)
        self.bus.subscribe("external_message", self._on_external)

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
        - trivial (< 0.2): direct execution, no thinking (max speed)
        - simple (0.2-0.5): direct execution + light self-verification
        - complex (0.5-0.8): clarification check + think + reflect + self-verify + final polish
        - expert (>= 0.8): everything + multi-agent + post-reflection + tool chain planning

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

        # --- Step 1: Clarification check (complex and above) ---
        # Short, unambiguous requests skip this even at complex tier
        if complexity >= CLARIFICATION_MIN_COMPLEXITY and self._needs_clarification_check(turn):
            if await self._try_clarification(messages, turn):
                # User needs to provide clarification — turn is done for now
                self.publish("turn_completed", turn=turn)
                return

        # --- Step 2: Pre-execution thinking by tier ---
        multi_agent_done = False
        if complexity >= EXPERT_COMPLEXITY_THRESHOLD:
            # Expert level: tool chain planning + multi-agent pattern
            if complexity >= TOOL_CHAIN_PLANNING_MIN_COMPLEXITY:
                await self._plan_tool_chain(messages, turn, tools)
            # multi-agent publishes turn_completed itself on success
            multi_agent_done = await self._multi_agent_phase(messages, turn)
        elif complexity >= COMPLEX_COMPLEXITY_THRESHOLD:
            # Complex level: think + reflect
            await self._think_phase(messages, turn)
            await self._reflect_phase(messages, turn)
        # else: simple/trivial — skip thinking entirely for speed

        # If multi-agent handled the turn, it already published turn_completed.
        # Skip the rest to avoid double-publishing and wasted work.
        if multi_agent_done:
            return

        # Context compression (always, but cheap when not needed)
        await self._compress_context(messages, turn)

        # --- Step 3: Tool-call loop ---
        await self._tool_loop(messages, turn, tools)

        # --- Step 4: Post-execution quality improvements by tier ---
        # For complex+: combine self-verification and final polish into one
        # LLM call when possible (saves one round-trip).
        if complexity >= FINAL_POLISH_MIN_COMPLEXITY and turn.result and not turn.error:
            await self._verify_and_polish(messages, turn, complexity)
        elif complexity >= SELF_VERIFY_MIN_COMPLEXITY and turn.result and not turn.error:
            # simple tier: light self-verification only
            await self._self_verify(messages, turn, complexity)

        if complexity >= POST_REFLECT_MIN_COMPLEXITY and turn.result:
            # Post-execution reflection: learn from this turn (expert only)
            await self._post_reflect(turn)

        # Auto-extract entities
        self._extract_entities(turn)

        self.publish("turn_completed", turn=turn)
        logger.info(
            "reply produced (%s mode, %d tokens, %.2fs)",
            "expert" if complexity >= EXPERT_COMPLEXITY_THRESHOLD
            else "complex" if complexity >= COMPLEX_COMPLEXITY_THRESHOLD
            else "simple" if complexity >= SIMPLE_COMPLEXITY_THRESHOLD
            else "trivial",
            turn.tokens_used,
            turn.duration_seconds or 0,
        )

    def _needs_clarification_check(self, turn: TurnContext) -> bool:
        """Heuristic: should we even bother asking the LLM if input is ambiguous?

        Short, specific questions (e.g. "现在几点", "1+1=") don't need the
        clarification check even at complex tier — it would just waste a call.
        """
        text = (turn.input_text or "").strip()
        # Very short inputs are usually clear commands or questions
        if len(text) < 15:
            return False
        # Inputs with code blocks, URLs, or file paths are usually concrete tasks
        if any(marker in text for marker in ("```", "http", "/workspace", ".py", ".js")):
            return False
        return True

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

            if not tool_calls:
                final_text = resp.get("text", "") or ""
                if final_text:
                    messages.append({"role": "assistant", "content": final_text})
                break

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

    # ------------------------------------------------------- smart boost

    def _lightweight_model(self, turn: TurnContext) -> str:
        """Pick the lightweight model for auxiliary calls when available.

        Auxiliary calls (clarification, self-check, polish, reflection, tool
        chain planning) don't need the full power of the primary model — using
        a cheaper/faster model here cuts cost and latency significantly.
        """
        if self.ctx and self.ctx.config:
            lw = self.ctx.config.get("llm", {}).get("lightweight_model")
            if lw:
                return lw
        return turn.model

    async def _llm_quick_call(
        self, prompt: str, turn: TurnContext, max_tokens: int = 200,
        use_lightweight: bool = True,
    ) -> Optional[str]:
        """One-shot LLM call for auxiliary tasks (no tools, no history).

        Centralizes the try/except + token accounting that was duplicated
        across 5 smart-boost methods. Returns the text reply or None on failure.
        """
        if self._llm is None:
            return None
        model = self._lightweight_model(turn) if use_lightweight else turn.model
        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                max_tokens=max_tokens,
                tools=None,
            )
            text = (resp.get("text") or "").strip()
            # Account for tokens used by auxiliary calls
            tokens = int(resp.get("tokens_used") or 0)
            if tokens:
                turn.tokens_used = (turn.tokens_used or 0) + tokens
            return text
        except Exception as exc:
            logger.debug("auxiliary LLM call failed: %s", exc)
            return None

    @staticmethod
    def _is_zh() -> bool:
        """Cached language check (zh vs en) for prompt selection."""
        from i18n import get_language
        return (get_language() or "zh").lower().startswith("zh")

    async def _try_clarification(
        self, messages: List[Dict[str, Any]], turn: TurnContext,
    ) -> bool:
        """Check if the user's request is ambiguous and ask for clarification.

        Only triggered for complex+ tasks. Returns True if clarification was
        asked (i.e. turn.result is set and the caller should stop early).
        Returns False if the task is clear enough to proceed normally.
        """
        if messages is None or turn is None:
            return False

        user_text = turn.input_text or ""
        zh = self._is_zh()

        # Quick heuristic: very short or ambiguous inputs likely need clarification
        ambiguous_keywords = (
            ["那个", "这个", "它", "帮我弄", "搞一下", "处理一下", "你看着办", "随便", "差不多"]
            if zh else
            ["do it", "fix it", "handle it", "that thing", "whatever"]
        )
        has_ambiguous_kw = any(kw in user_text.lower() for kw in ambiguous_keywords)
        # Only do LLM-based check for medium-length inputs that might be ambiguous
        if not has_ambiguous_kw and len(user_text) >= 15 and len(user_text) < 50:
            return False

        # Use a lightweight LLM call to judge ambiguity and generate questions
        if zh:
            check_prompt = (
                f"请判断以下用户请求是否存在明显的歧义或信息不足，"
                f"导致你无法准确执行。\n\n"
                f"用户请求：\"{user_text[:500]}\"\n\n"
                f"如果有歧义，请提出 1-3 个最关键的澄清问题，"
                f"用编号列表形式输出。\n"
                f"如果没有歧义或信息足够，请只回答『没问题』三个字。"
            )
        else:
            check_prompt = (
                f"Determine if the following user request has significant ambiguity "
                f"or missing information that prevents you from executing it accurately.\n\n"
                f"User request: \"{user_text[:500]}\"\n\n"
                f"If ambiguous, ask 1-3 key clarification questions as a numbered list.\n"
                f"If clear enough, reply with only the word 'CLEAR'."
            )

        reply = await self._llm_quick_call(check_prompt, turn, max_tokens=200)
        if not reply:
            return False

        is_clear = (
            reply.startswith("没问题") or
            reply.upper().startswith("CLEAR") or
            len(reply) < 10
        )
        if is_clear:
            return False

        # Model has clarification questions — present them to the user
        intro = (
            "在开始之前，我需要确认几个问题，以确保给你最准确的结果：\n\n"
            if zh else
            "Before I start, I need to clarify a few things to give you the best result:\n\n"
        )
        turn.record_success(intro + reply, 0)  # tokens already accounted in _llm_quick_call
        turn.meta["clarification_asked"] = True
        logger.info("clarification asked for ambiguous request (%d chars)", len(reply))
        return True

    async def _self_verify(
        self, messages: List[Dict[str, Any]], turn: TurnContext, complexity: float,
    ) -> None:
        """Verify that the answer actually addresses the user's question.

        For simple tasks: quick check (did I answer the question?)
        For complex+: deeper verification (facts, logic, completeness)

        If issues are found, we inject a correction hint and do one more
        tool-loop iteration to fix it.
        """
        if not turn.result:
            return

        zh = self._is_zh()
        answer = turn.result or ""
        question = turn.input_text or ""

        # Lightweight check for simple tasks: did we actually answer?
        is_deep = complexity >= COMPLEX_COMPLEXITY_THRESHOLD

        if zh:
            if is_deep:
                verify_prompt = (
                    "【内部自检 — 不要输出给用户】\n\n"
                    "请检查以下回答是否正确、完整地回应了用户的问题。"
                    "从三个维度评分（每项0-10分）：\n"
                    "1. 相关性：回答是否紧扣问题，没有答非所问\n"
                    "2. 准确性：事实是否正确，逻辑是否自洽\n"
                    "3. 完整性：是否覆盖了问题的所有方面\n\n"
                    f"用户问题：{question[:300]}\n\n"
                    f"当前回答：{answer[:800]}\n\n"
                    "如果三项都>=8分，只回答『通过』。\n"
                    "如果有问题，用一句话指出最严重的问题是什么，以及如何改进。"
                )
            else:
                verify_prompt = (
                    "【快速自检】这个回答是否直接回答了用户的问题？"
                    "用『是』或『否』回答，不要解释。\n\n"
                    f"问题：{question[:200]}\n"
                    f"回答：{answer[:500]}"
                )
        else:
            if is_deep:
                verify_prompt = (
                    "[Internal self-check — DO NOT show to user]\n\n"
                    "Check if the following answer correctly and completely addresses "
                    "the user's question. Rate 3 dimensions (0-10 each):\n"
                    "1. Relevance: does it answer the question, not something else?\n"
                    "2. Accuracy: are facts correct and logic consistent?\n"
                    "3. Completeness: does it cover all aspects of the question?\n\n"
                    f"User question: {question[:300]}\n\n"
                    f"Current answer: {answer[:800]}\n\n"
                    "If all three >= 8, reply with only 'PASS'.\n"
                    "If there are issues, state the most serious problem in one sentence "
                    "and how to fix it."
                )
            else:
                verify_prompt = (
                    "[Quick self-check] Does this answer directly address the user's question? "
                    "Reply with only YES or NO, no explanation.\n\n"
                    f"Question: {question[:200]}\n"
                    f"Answer: {answer[:500]}"
                )

        result = await self._llm_quick_call(verify_prompt, turn, max_tokens=150)
        if not result:
            return

        passed = (
            result.startswith("通过") or
            result.upper().startswith("PASS") or
            result.startswith("是") or
            result.upper().startswith("YES")
        )

        if passed:
            turn.meta["self_verify"] = "passed"
            logger.debug("self-verification passed")
            return

        # Self-verification found issues — inject correction and do one more iteration
        turn.meta["self_verify"] = "corrected"
        turn.meta["self_verify_issue"] = result
        logger.info("self-verification found issues, correcting: %s", result[:100])

        correction_msg = (
            f"[自检发现问题：{result}]\n\n"
            "请根据以上反馈修正你的答案，使其更准确、更完整。"
            "如果需要，可以继续调用工具。"
            if zh else
            f"[Self-check found issue: {result}]\n\n"
            "Please revise your answer based on this feedback to make it more "
            "accurate and complete. You may continue using tools if needed."
        )

        messages.append({"role": "user", "content": correction_msg})

        # Run one more tool-loop iteration to fix the answer
        tools = self._prepare_tools(turn)
        prev_result = turn.result
        await self._tool_loop(messages, turn, tools)

        # If the correction didn't produce a better result, keep the original
        if not turn.result or len(turn.result) < len(prev_result or "") // 2:
            turn.result = prev_result

    async def _verify_and_polish(
        self, messages: List[Dict[str, Any]], turn: TurnContext, complexity: float,
    ) -> None:
        """Combined self-verification + final polish in ONE LLM call.

        For complex+ tasks this saves a round-trip vs running _self_verify
        then _final_polish separately. The model is asked to:
          1. Check the answer for issues (relevance/accuracy/completeness)
          2. If OK, output the polished version directly
          3. If issues found, output the corrected + polished version

        If the answer is very short, skip polishing (only verify).
        """
        if not turn.result:
            return

        zh = self._is_zh()
        answer = turn.result or ""
        question = turn.input_text or ""

        # Short answers: only verify, skip polish (saves tokens)
        skip_polish = len(answer) < 200

        if zh:
            if skip_polish:
                prompt = (
                    "【内部自检 — 不要输出给用户】\n\n"
                    "请检查以下回答是否正确回答了用户的问题。"
                    "如果正确，只回答『通过』。如果有问题，用一句话指出并给出修正后的答案。\n\n"
                    f"用户问题：{question[:300]}\n\n"
                    f"当前回答：{answer[:800]}"
                )
            else:
                prompt = (
                    "【自检并优化 — 内部使用】\n\n"
                    "请对以下回答执行两步操作：\n"
                    "1. 自检：回答是否正确、完整地回应了用户问题？\n"
                    "2. 优化：在保持核心内容不变的前提下，优化结构（分点/表格）、"
                    "精炼语言、突出重点、长答案加要点总结。\n\n"
                    f"用户问题：{question[:300]}\n\n"
                    f"原始回答：\n{answer[:2000]}\n\n"
                    "请直接输出优化后的完整回答。如果原回答有明显错误，请在优化时一并修正。"
                )
        else:
            if skip_polish:
                prompt = (
                    "[Internal self-check — DO NOT show to user]\n\n"
                    "Check if the following answer correctly addresses the user's question. "
                    "If correct, reply with only 'PASS'. If there are issues, state the problem "
                    "in one sentence and provide the corrected answer.\n\n"
                    f"User question: {question[:300]}\n\n"
                    f"Current answer: {answer[:800]}"
                )
            else:
                prompt = (
                    "[Self-check and polish — internal use]\n\n"
                    "Perform two steps on the following answer:\n"
                    "1. Self-check: does it correctly and completely address the question?\n"
                    "2. Polish: keeping core content unchanged, improve structure "
                    "(headings/bullets/tables), tighten language, highlight key points, "
                    "add a brief summary for long answers.\n\n"
                    f"User question: {question[:300]}\n\n"
                    f"Original answer:\n{answer[:2000]}\n\n"
                    "Output the full polished answer directly. Fix any obvious errors while polishing."
                )

        max_tok = 150 if skip_polish else min(len(answer) + 500, 4000)
        result = await self._llm_quick_call(prompt, turn, max_tokens=max_tok)
        if not result:
            return

        if skip_polish:
            # Verify-only path
            passed = (
                result.startswith("通过") or
                result.upper().startswith("PASS") or
                result.startswith("是") or
                result.upper().startswith("YES")
            )
            if passed:
                turn.meta["self_verify"] = "passed"
            else:
                turn.meta["self_verify"] = "corrected"
                turn.meta["self_verify_issue"] = result
                logger.info("self-verification found issues: %s", result[:100])
        else:
            # Combined verify+polish path
            if result and len(result) > len(answer) // 2:
                turn.result = result
                turn.meta["polished"] = True
                turn.meta["self_verify"] = "passed"
                logger.debug(
                    "verify+polish applied (%d -> %d chars)", len(answer), len(result)
                )

    async def _final_polish(
        self, messages: List[Dict[str, Any]], turn: TurnContext,
    ) -> None:
        """Polish the final answer for better structure, clarity, and readability.

        Kept for backward compatibility / standalone use. The combined
        _verify_and_polish is preferred for complex+ tasks.
        """
        if not turn.result:
            return

        zh = self._is_zh()
        answer = turn.result or ""

        # Skip very short answers (they don't need polishing)
        if len(answer) < 200:
            return

        if zh:
            polish_prompt = (
                "【答案优化 — 不要改变核心内容，只优化表达】\n\n"
                "请对以下回答进行优化，要求：\n"
                "1. 结构清晰：使用合适的标题、分点、表格等组织内容\n"
                "2. 语言精炼：去除冗余，表达更专业、清晰\n"
                "3. 重点突出：关键信息放在显眼位置\n"
                "4. 结尾总结：长答案加一个简短的要点总结\n\n"
                f"用户的问题：{turn.input_text[:200]}\n\n"
                f"原始回答：\n{answer[:2000]}\n\n"
                "请输出优化后的完整回答。"
            )
        else:
            polish_prompt = (
                "[Answer polishing — don't change core content, only improve presentation]\n\n"
                "Please polish the following answer:\n"
                "1. Clear structure: use headings, bullet points, tables where appropriate\n"
                "2. Concise language: remove redundancy, make it more professional and clear\n"
                "3. Highlight key points: put important info in prominent positions\n"
                "4. Summary: add a brief key-takeaways section for long answers\n\n"
                f"User question: {turn.input_text[:200]}\n\n"
                f"Original answer:\n{answer[:2000]}\n\n"
                "Output the full polished answer."
            )

        polished = await self._llm_quick_call(
            polish_prompt, turn, max_tokens=min(len(answer) + 500, 4000)
        )
        if polished and len(polished) > len(answer) // 2:
            turn.result = polished
            turn.meta["polished"] = True
            logger.debug("final polish applied (%d -> %d chars)", len(answer), len(polished))

    async def _post_reflect(self, turn: TurnContext) -> None:
        """Post-execution reflection for expert-tier tasks.

        After completing a complex task, review what went well and what could
        be improved. The insights are stored in turn.meta for the self-improvement
        system to learn from.

        This is a meta-cognitive step that helps the agent get better over time.
        """
        if turn is None:
            return

        zh = self._is_zh()
        answer = turn.result or ""
        question = turn.input_text or ""
        tokens_used = turn.tokens_used or 0
        duration = turn.duration_seconds or 0

        if zh:
            reflect_prompt = (
                "【执行后复盘 — 内部使用，不要输出给用户】\n\n"
                "请对刚刚完成的任务进行复盘：\n\n"
                f"任务：{question[:300]}\n"
                f"回答长度：{len(answer)} 字\n"
                f"消耗 token：{tokens_used}\n"
                f"用时：{duration:.1f} 秒\n\n"
                "请回答以下问题（简短回答）：\n"
                "1. 执行过程中最有效的一步是什么？\n"
                "2. 最大的弯路或浪费在哪里？\n"
                "3. 如果重做一次，你会怎么改进？\n"
                "4. 这个回答的质量打几分（0-10）？为什么？"
            )
        else:
            reflect_prompt = (
                "[Post-execution review — internal use only, do not show to user]\n\n"
                "Review the task you just completed:\n\n"
                f"Task: {question[:300]}\n"
                f"Answer length: {len(answer)} chars\n"
                f"Tokens used: {tokens_used}\n"
                f"Duration: {duration:.1f}s\n\n"
                "Answer these questions briefly:\n"
                "1. What was the most effective step in execution?\n"
                "2. What was the biggest detour or waste?\n"
                "3. If you did it again, how would you improve?\n"
                "4. Rate the answer quality (0-10) and why?"
            )

        reflection = await self._llm_quick_call(reflect_prompt, turn, max_tokens=300)
        if reflection:
            turn.meta["post_reflection"] = reflection
            logger.debug("post-reflection completed (%d chars)", len(reflection))

    async def _plan_tool_chain(
        self, messages: List[Dict[str, Any]], turn: TurnContext, tools: List[Dict[str, Any]],
    ) -> None:
        """Plan a tool execution chain for expert-tier tasks.

        Instead of the model discovering tools one by one in the loop,
        we pre-plan the optimal tool sequence. This reduces wasted tool
        calls and makes execution more strategic.

        The plan is injected into messages as guidance for the tool loop.
        """
        if not tools:
            return

        zh = self._is_zh()
        tool_names = [
            t.get("function", {}).get("name", "") for t in tools
            if isinstance(t, dict)
        ]
        tool_list = ", ".join(tool_names[:15])

        if zh:
            plan_prompt = (
                "【工具链规划 — 内部使用】\n\n"
                f"可用工具：{tool_list}\n\n"
                f"用户任务：{turn.input_text[:300]}\n\n"
                "请规划一个最优的工具调用顺序：\n"
                "1. 哪些工具应该被调用？\n"
                "2. 调用顺序是什么？哪些可以并行？\n"
                "3. 每个工具的预期输入输出是什么？\n"
                "4. 如果某个工具失败了，替代方案是什么？\n\n"
                "用编号列表输出规划结果。"
            )
        else:
            plan_prompt = (
                "[Tool chain planning — internal use]\n\n"
                f"Available tools: {tool_list}\n\n"
                f"User task: {turn.input_text[:300]}\n\n"
                "Plan the optimal tool call sequence:\n"
                "1. Which tools should be called?\n"
                "2. In what order? Which can run in parallel?\n"
                "3. What's the expected input/output for each tool?\n"
                "4. What's the fallback if a tool fails?\n\n"
                "Output as a numbered list."
            )

        plan = await self._llm_quick_call(
            plan_prompt, turn, max_tokens=400,
            # Tool chain planning benefits from the full model
            use_lightweight=False,
        )
        if plan:
            header = "【工具链执行规划】\n" if zh else "[Tool chain plan]\n"
            messages.append({"role": "assistant", "content": header + plan})
            prompt = (
                "好。按照这个规划执行工具调用。"
                if zh else
                "Good. Execute tool calls following this plan."
            )
            messages.append({"role": "user", "content": prompt})
            turn.meta["tool_chain_plan"] = plan
            logger.debug("tool chain plan created (%d chars)", len(plan))
