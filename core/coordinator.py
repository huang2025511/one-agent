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
import re
import time
from typing import Any, Dict, List, Optional

from core.context import TurnContext
from core.events import Event
from core.plugin import Plugin
from core.tool_result import ToolResult
from i18n import get_language
from models import LLMProvider
from router import DEFAULT_COMPLEX_THRESHOLD, DEFAULT_SIMPLE_THRESHOLD, DEFAULT_TRIVIAL_THRESHOLD
from skills import SkillManager

# Intelligent features
from core.sentiment import get_sentiment_analyzer
from core.suggestions import get_suggestion_engine
from memory.user_profile import get_profile_store
from core.metacognition import get_metacognition_engine
from core.reasoning import get_step_reasoner
from core.style_adapter import StyleAdapter
from core.failure_recovery import get_failure_recovery
from memory.dialog_summary import get_dialog_summarizer
from core.safety import scan_input, scan_output
from core.tool_cache import get_tool_cache
from core.rate_limiter import get_rate_limiter
from models.tiers import MODEL_COST

# Round 6: new intelligent features
from core.eval import get_eval_harness
from core.batch import get_batch_processor
from core.deep_research import get_deep_researcher
from core.model_compare import get_model_comparer

# Round 7: production reliability
from core.circuit_breaker import get_circuit_manager, CircuitOpenError
from core.backoff import llm_backoff
from core.alerting import get_alert_manager, AlertSeverity

# Round 7: tool ecosystem
from skills.email import get_email_skill
from skills.calendar import get_calendar_skill
from skills.database import get_database_skill
from skills.mcp_server import get_mcp_server
from skills.openapi import get_openapi_skill

# Round 7: intelligence depth
from core.rag_advanced import get_advanced_rag
from core.agent_mesh import get_agent_mesh
from core.workflow_engine import get_workflow_engine
from core.chart_gen import get_chart_generator
from core.conv_branch import get_branch_manager

logger = logging.getLogger(__name__)

# Coordinator configuration constants
MAX_TOOL_ITERATIONS = 5
DEFAULT_MAX_TOKENS = 2048
MAX_SKILL_FAILURES = 3
TURN_COMPLETION_TIMEOUT = 120.0
SKILL_EXECUTION_TIMEOUT = 60.0
# Complexity tier thresholds (determine execution strategy).
# Imported from router to keep a single source of truth — router owns
# task classification, coordinator consumes the same thresholds.
EXPERT_COMPLEXITY_THRESHOLD = DEFAULT_COMPLEX_THRESHOLD     # >= → multi-agent pattern
COMPLEX_COMPLEXITY_THRESHOLD = DEFAULT_SIMPLE_THRESHOLD     # >= → think + reflect
SIMPLE_COMPLEXITY_THRESHOLD = DEFAULT_TRIVIAL_THRESHOLD     # >= → light self-verification
# Smart boost feature flags by complexity tier
# - trivial (<0.2): direct execution, no enhancements (max speed)
# - simple (0.2-0.5): light thinking only if needed + skip self-verification (speed optimized)
# - complex (0.5-0.8): full pre-thinking + reflection + self-verification + final polish + clarification
# - expert (>=0.8): everything + post-execution review + tool chain planning
THINK_MIN_COMPLEXITY = 0.3            # raised from 0.2 — reduce overhead for very simple tasks
SELF_VERIFY_MIN_COMPLEXITY = 0.35     # raised from 0.2 — skip verification for most simple tasks
CLARIFICATION_MIN_COMPLEXITY = 0.5      # complex and above
FINAL_POLISH_MIN_COMPLEXITY = 0.5     # complex and above
POST_REFLECT_MIN_COMPLEXITY = 0.8       # expert only
TOOL_CHAIN_PLANNING_MIN_COMPLEXITY = 0.8  # expert only

# Round 6: new feature constants
MAX_TOOL_RETRIES = 2                    # auto-retry failed tool calls with corrected args
REGENERATION_MAX = 3                    # max rewrite_turn regenerations per turn
DYNAMIC_TEMP_BASE = 0.7                 # base temperature for dynamic adjustment
DYNAMIC_TEMP_FACTUAL = 0.1              # low temp for factual tasks
DYNAMIC_TEMP_CREATIVE = 0.9             # high temp for creative tasks
DEEP_RESEARCH_MIN_COMPLEXITY = 0.7      # auto-trigger deep research at this complexity


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
        # Intelligent features (lazy-loaded)
        self._sentiment: Optional[Any] = None
        self._suggestions: Optional[Any] = None
        self._profile: Optional[Any] = None
        self._metacognition: Optional[Any] = None
        self._reasoner: Optional[Any] = None
        self._style_adapter: Optional[StyleAdapter] = None
        self._failure_recovery: Optional[Any] = None
        self._dialog_summarizer: Optional[Any] = None
        # Round 6: new feature instances
        self._eval: Optional[Any] = None
        self._batch: Optional[Any] = None
        self._deep_researcher: Optional[Any] = None
        self._model_comparer: Optional[Any] = None
        # Track regeneration count per session
        self._regeneration_count: Dict[str, int] = {}
        # Track proactive plans per session
        self._proactive_plans: Dict[str, List[str]] = {}
        # Round 7: new feature instances
        self._circuit_mgr: Optional[Any] = None
        self._alert_mgr: Optional[Any] = None
        self._email_skill: Optional[Any] = None
        self._calendar_skill: Optional[Any] = None
        self._database_skill: Optional[Any] = None
        self._mcp_server: Optional[Any] = None
        self._openapi_skill: Optional[Any] = None
        self._advanced_rag: Optional[Any] = None
        self._agent_mesh: Optional[Any] = None
        self._workflow_engine: Optional[Any] = None
        self._chart_gen: Optional[Any] = None
        self._branch_mgr: Optional[Any] = None

    # ------------------------------------------------------------ setup
    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self.bus.subscribe("turn_routed", self._on_routed)
        self.bus.subscribe("external_message", self._on_external)

        # 注册 followup_check 到 AsyncTaskScheduler
        # 之前只调度不注册，导致延迟任务执行时报 "Unknown function: followup_check"
        try:
            from core.task_scheduler import get_task_scheduler
            scheduler = get_task_scheduler()
            scheduler.register("followup_check", self._followup_check_handler)
            logger.info("coordinator: registered followup_check task function")
        except Exception as exc:
            logger.debug("coordinator: failed to register followup_check: %s", exc)

    def bind(self, llm: LLMProvider, skills: SkillManager) -> None:
        self._llm = llm
        self._skills = skills

    def _emit_progress(self, turn: TurnContext, message: str, phase: str = "") -> None:
        """向网关发布实时进度事件，让用户知道 agent 正在做什么。

        避免长时间沉默无反馈。网关会根据 session_id 过滤并做速率控制。
        """
        try:
            self.publish(
                "turn_progress",
                session_id=turn.session_id,
                message=message,
                phase=phase,
            )
        except Exception:
            pass  # 进度事件失败不影响主流程

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
                result = await asyncio.wait_for(
                    self._skills.dispatch(name, args),
                    timeout=SKILL_EXECUTION_TIMEOUT,
                )
                if isinstance(result, ToolResult):
                    result_str = str(result.data) if result.data is not None else str(result)
                    is_error = result.status in ("error", "unavailable")
                else:
                    result_str = str(result)
                    is_error = False
            else:
                result_str = "[no skill manager bound]"
                is_error = False
        except asyncio.TimeoutError:
            logger.error("skill dispatch timeout: %s(%s)", name, args)
            duration_ms = (time.time() - start) * 1000
            failed_skills[name] = failed_skills.get(name, 0) + 1
            return ToolResult(
                tool_name=name,
                status="error",
                error=f"工具调用超时（{int(SKILL_EXECUTION_TIMEOUT)}秒），请稍后重试或直接用已有知识回答。",
                duration_ms=duration_ms,
            )
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
            truncated=len(result_str) > 3000,
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

    async def _compress_messages(self, messages: list, turn, extra_hints: Optional[List[str]] = None) -> str:
        """Use a lightweight LLM call to summarize early conversation.

        Args:
            messages: 对话消息列表
            turn: 当前 turn context
            extra_hints: ContextCompressor 提取的关键信息，注入到 prompt 中
        """
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
        # Round 8：如果 ContextCompressor 提取了关键事实，作为 hint 注入 prompt
        # 确保 LLM 摘要不会丢失这些关键信息
        system_content = "你是对话摘要助手。用2-3句话总结以下对话的关键信息、用户需求和已完成的步骤。只输出摘要，不要加任何前缀。"
        if extra_hints:
            hints_text = "\n".join(f"- {h}" for h in extra_hints[:5])
            system_content += f"\n\n已通过重要性评分识别出的关键信息（务必保留）：\n{hints_text}"
        prompt = [
            {"role": "system", "content": system_content},
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
        lang = "zh" if self._is_zh() else "en"

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
        """Verify password and enable OS mode for this session.

        When no password is configured (security.system_executor_password is empty),
        OS mode is automatically enabled since SAFE commands don't require password.
        This allows the system to automatically enter OS mode when the router
        detects system operation needs, without requiring user password input.
        """
        if self._skills is None:
            return False
        try:
            # Check if password is configured
            has_password = False
            if self.ctx and hasattr(self.ctx, "config"):
                sec_cfg = getattr(self.ctx.config, "get", lambda k, d: d)("security", {})
                stored_hash = str(sec_cfg.get("system_executor_password", "") or "")
                has_password = bool(stored_hash) and (
                    len(stored_hash) == 64 or stored_hash.startswith("pbkdf2_sha256$")
                )

            if not has_password:
                logger.info("OS mode auto-enabled (no password configured)")
                return True

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
        # ---------- 模型管理 ----------
        "添加模型": "model_manage", "addmodel": "model_manage", "add-model": "model_manage",
        "显示模型": "model_manage", "showmodels": "model_manage", "show-models": "model_manage",
        "models": "model_manage", "模型列表": "model_manage",
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
        # ---------- Round 6: 新功能 ----------
        "retry": "_rewrite", "重试": "_rewrite", "重新生成": "_rewrite", "换个说法": "_rewrite",
        "regenerate": "_rewrite", "redo": "_rewrite", "再来一次": "_rewrite",
        "deep": "_deep_research", "深度研究": "_deep_research", "深入研究": "_deep_research",
        "deep-research": "_deep_research", "research": "_deep_research",
        "batch": "_batch", "批量": "_batch", "批量处理": "_batch", "批量任务": "_batch",
        "compare": "_model_compare", "对比": "_model_compare", "模型对比": "_model_compare",
        "compare-models": "_model_compare", "ab": "_model_compare",
        "eval": "_eval", "评估": "_eval", "evaluate": "_eval", "评分": "_eval",
        # ---------- Round 7: 新功能命令 ----------
        # Email & Calendar
        "email": "_email", "邮件": "_email", "mail": "_email",
        "calendar": "_calendar", "日历": "_calendar", "日程": "_calendar",
        # Database
        "db": "_db", "database": "_db", "数据库": "_db", "sql": "_db",
        # MCP & OpenAPI
        "mcp": "_mcp", "mcp_server": "_mcp",
        "openapi": "_openapi", "api": "_openapi",
        # Agent mesh & workflow
        "mesh": "_agent_mesh", "agent_mesh": "_agent_mesh", "multi_agent": "_agent_mesh",
        "workflow": "_workflow", "wfl": "_workflow", "流程": "_workflow",
        # Charts & branching
        "chart": "_chart", "图表": "_chart", "diagram": "_chart", "mermaid": "_chart",
        "branch": "_branch", "分支": "_branch", "fork": "_branch",
        "branch_switch": "_branch_switch", "切换分支": "_branch_switch",
        "branch_list": "_branch_list", "分支列表": "_branch_list",
    }

    async def _maybe_direct_skill_dispatch(self, turn: TurnContext) -> bool:
        """自然语言→skill 直通调度。

        检测用户消息是否表达某个 skill 能处理的明确意图（无需 LLM tool-calling），
        若命中则直接 dispatch skill 并返回 True。

        覆盖的意图：
        - "拉取/添加/刷新模型" → model_manage skill（复用已配置 key）
        - "搜一下/搜索 X/查一下" → web_search skill
        - "现在几点/当前时间" → now skill
        - "算一下 X" / 纯算式 → calc skill
        - "记一下 X/保存笔记" → save_note skill
        - "执行命令 X/运行 X" → system_run skill（需密码，仅简单只读命令直通）

        设计原因：弱模型（如 flash-lite）不支持 tool calling，自然语言请求
        会被路由到这些模型 → 无法触发 skill → 只能打官腔。直通调度绕过这个限制。
        """
        text = (turn.input_text or "").strip()
        if not text:
            return False

        if self._skills is None:
            return False

        # 1) 模型拉取意图（最特定，先检查）
        try:
            from skills import _try_natural_language_fetch
        except ImportError:
            _try_natural_language_fetch = None  # type: ignore

        if _try_natural_language_fetch is not None and self._llm is not None:
            nl_result = _try_natural_language_fetch(self._llm, text)
            if nl_result is not None:
                provider, free_only, all_models_flag = nl_result
                logger.info(
                    "coordinator: 自然语言直通 model_manage (provider=%s)", provider,
                )
                self._emit_progress(turn, f"正在从 {provider} 拉取模型列表...", "skill_dispatch")
                try:
                    result = await self._skills.dispatch("model_manage", {"input": text})
                    turn.result = str(result) if result else "（无结果）"
                    turn.meta["direct_skill_dispatch"] = "model_manage"
                except Exception as exc:
                    logger.exception("coordinator: 自然语言直通 model_manage 失败: %s", exc)
                    turn.record_failure(f"模型拉取失败: {exc}")
                self.publish("turn_completed", turn=turn)
                return True

        # 2) 其他高频 skill 意图
        skill_id, skill_args, progress_msg = self._match_nl_skill_intent(text)
        if skill_id is None:
            return False

        logger.info("coordinator: 自然语言直通 %s", skill_id)
        if progress_msg:
            self._emit_progress(turn, progress_msg, "skill_dispatch")
        try:
            result = await self._skills.dispatch(skill_id, skill_args)
            turn.result = str(result) if result else "（无结果）"
            turn.meta["direct_skill_dispatch"] = skill_id
        except Exception as exc:
            logger.exception("coordinator: 自然语言直通 %s 失败: %s", skill_id, exc)
            turn.record_failure(f"{skill_id} 执行失败: {exc}")
        self.publish("turn_completed", turn=turn)
        return True

    def _match_nl_skill_intent(self, text: str):
        """识别自然语言意图并映射到 skill。返回 (skill_id, args, progress_msg) 或 (None, None, None)。

        只有意图非常明确时才命中（避免误触发）。模糊请求交给 LLM tool-calling。
        """
        t = text.strip().lower()
        if not t:
            return None, None, None

        # ---- now：当前时间 ----
        # 触发：完全匹配或"现在几点/当前时间/今天日期"等
        now_patterns = (
            "现在几点", "当前时间", "现在时间", "今天日期", "今天几号",
            "what time", "current time", "now time", "今天星期",
        )
        if t in ("几点了", "时间", "now", "时间?", "时间？", "几点", "/now", "/时间"):
            return "now", {"input": text}, None
        for p in now_patterns:
            if p in t:
                return "now", {"input": text}, None

        # ---- calc：纯算式或"算一下" ----
        # 触发：纯算式（只含数字+运算符）或"算一下/计算 X"
        import re as _re
        # 纯算式（长度>2，只含数字和运算符）
        if len(text) > 2 and _re.fullmatch(r"[0-9+\-*/(). ]+", text.strip()):
            return "calc", {"input": text.strip()}, None
        calc_patterns = ("算一下", "计算一下", "算算", "帮我算", "calculate", "compute")
        for p in calc_patterns:
            if p in t:
                # 提取算式部分
                expr = text.split(p, 1)[-1].strip(" ，,。.")
                if expr and _re.fullmatch(r"[0-9+\-*/(). ]+", expr):
                    return "calc", {"input": expr}, None

        # ---- web_search：搜索意图 ----
        # 触发："搜一下 X/搜索 X/查一下 X/帮我查"（需要 X 有实际内容）
        search_patterns = ("搜一下", "搜索一下", "搜索", "帮我搜", "查一下", "帮我查",
                           "查找一下", "查一查", "search for", "google一下")
        for p in search_patterns:
            if t.startswith(p):
                query = text[len(p):].strip(" ，,。.?？")
                if len(query) >= 2:
                    return "web_search", {"input": query}, f"正在搜索：{query[:30]}..."

        # ---- save_note：记笔记 ----
        # 触发："记一下 X/记住 X/保存笔记/帮我记"
        note_patterns = ("记一下", "记住", "帮我记", "保存笔记", "记笔记", "记下")
        for p in note_patterns:
            if t.startswith(p):
                content = text[len(p):].strip(" ，,。.")
                if len(content) >= 2:
                    return "save_note", {"input": content}, None

        # ---- system_run：执行命令（仅安全只读命令直通，避免危险操作）----
        # 触发："执行命令 X/运行命令 X/跑一下 X"（X 是明确的命令）
        # 安全限制：只对 ls/cat/echo/date/whoami/pwd/df 等只读命令直通
        sys_patterns = ("执行命令", "运行命令", "跑一下命令", "帮我执行", "运行一下")
        for p in sys_patterns:
            if t.startswith(p):
                cmd = text[len(p):].strip(" ，,。.`")
                # 只对安全只读命令直通，避免危险操作
                safe_starts = ("ls", "cat", "echo", "date", "whoami", "pwd",
                               "df", "du", "free", "uptime", "uname", "head",
                               "tail", "wc", "find", "grep", "git status",
                               "git log", "git diff")
                if cmd and any(cmd.lower().startswith(s) for s in safe_starts):
                    return "system_run", {"input": cmd}, f"正在执行：{cmd[:30]}..."

        return None, None, None

    async def _maybe_auto_special_task(self, turn: TurnContext) -> bool:
        """傻瓜化：检测自然语言中的批量任务/模型对比意图，自动调用对应处理器。

        让用户无需知道 /batch、/compare 等斜杠命令——只要输入符合模式就自动触发：
        - 批量任务：包含任务动词（翻译/总结/分类/提取）+ 编号列表（3 项以上）
        - 模型对比：含"对比/比较" + 两个 provider/model token（带 /）+ 问题
        """
        text = (turn.input_text or "").strip()
        if not text or len(text) < 10:
            return False

        import re as _re

        # ---- 批量任务自动检测 ----
        # 触发条件：任务动词 + 至少 3 个编号列表项（1. 2. 3. 或 1、 2、 3、）
        batch_verbs = {
            "翻译": "translate", "translate": "translate",
            "总结": "summarize", "摘要": "summarize", "summarize": "summarize",
            "分类": "classify", "classify": "classify",
            "提取": "extract", "extract": "extract",
        }
        text_lower = text.lower()
        matched_verb = None
        for verb, task_type in batch_verbs.items():
            if verb in text_lower or verb in text:
                matched_verb = (verb, task_type)
                break

        if matched_verb is not None:
            # 检测编号列表项：1. / 1、 / 1) / - / * 开头的行
            list_item_pattern = _re.compile(
                r"^\s*(?:\d+[.、)]\s+|[-*]\s+).+", _re.MULTILINE
            )
            list_items = list_item_pattern.findall(text)
            if len(list_items) >= 3:
                _, task_type = matched_verb
                # 构造 args_text：<task_type>\n<原始内容>
                args_text = f"{task_type}\n{text}"
                self._emit_progress(turn, f"检测到批量{matched_verb[0]}任务，自动处理 {len(list_items)} 项...", "batch")
                try:
                    await self._handle_batch(turn, args_text)
                    if turn.result:
                        turn.meta["auto_batch_triggered"] = True
                        self.publish("turn_completed", turn=turn)
                        return True
                except Exception as exc:
                    logger.debug("auto batch failed, falling back: %s", exc)

        # ---- 模型对比自动检测 ----
        # 触发条件：含"对比/比较" + 两个 provider/model 格式的 token（含 /）+ 问题
        compare_keywords = ("对比一下", "比较一下", "对比", "比较", "vs", "versus", "compare")
        if any(kw in text_lower for kw in compare_keywords):
            # 提取所有 provider/model 格式的 token（如 openai/gpt-4o）
            model_tokens = _re.findall(r"[a-zA-Z][\w.-]*/[\w.-]+", text)
            if len(model_tokens) >= 2:
                model_a, model_b = model_tokens[0], model_tokens[1]
                # 问题是去掉关键词和两个模型名后的剩余文本
                question = text
                for kw in compare_keywords:
                    question = question.replace(kw, " ")
                question = question.replace(model_a, " ").replace(model_b, " ")
                question = _re.sub(r"\s+", " ", question).strip(" ，,。.?？")
                # 如果没有显式问题，复用上一轮输入
                if len(question) < 5:
                    history = turn.meta.get("history", [])
                    if history:
                        question = str(history[-1].get("input", ""))[:500]
                if len(question) >= 5:
                    args_text = f"{model_a} {model_b} {question}"
                    self._emit_progress(turn, f"检测到模型对比意图，对比 {model_a} vs {model_b}...", "model_compare")
                    try:
                        await self._handle_model_compare(turn, args_text)
                        if turn.result:
                            turn.meta["auto_compare_triggered"] = True
                            self.publish("turn_completed", turn=turn)
                            return True
                    except Exception as exc:
                        logger.debug("auto compare failed, falling back: %s", exc)

        # ---- 图表生成自动检测 ----
        # 触发条件：含"画/生成/做一个 + 图表类型关键词"，用 LLM 解析用户意图
        # 自动生成 Mermaid 图表。之前 /chart 只能手动输入 JSON 数据，
        # 用户不知道也不会用。现在自然语言即可触发。
        chart_verbs = ("画一个", "画个", "画", "生成一个", "生成个", "生成", "做一个",
                       "创建", "create", "draw", "generate", "make a")
        chart_types_cn = {
            "流程图": "flowchart", "流程": "flowchart",
            "时序图": "sequence", "序列图": "sequence",
            "饼图": "pie", "饼状图": "pie",
            "甘特图": "gantt",
            "时间线": "timeline", "时间轴": "timeline",
            "思维导图": "mindmap", "脑图": "mindmap",
            "柱状图": "bar", "条形图": "bar",
            "折线图": "line",
        }
        chart_types_en = {
            "flowchart": "flowchart", "sequence diagram": "sequence",
            "pie chart": "pie", "pie": "pie",
            "gantt chart": "gantt", "gantt": "gantt",
            "timeline": "timeline",
            "mind map": "mindmap", "mindmap": "mindmap",
            "bar chart": "bar", "bar": "bar",
            "line chart": "line", "line": "line",
        }
        has_verb = any(v in text_lower for v in chart_verbs)
        if has_verb:
            chart_type = None
            for kw, ct in chart_types_cn.items():
                if kw in text:
                    chart_type = ct
                    break
            if not chart_type:
                for kw, ct in chart_types_en.items():
                    if kw in text_lower:
                        chart_type = ct
                        break
            if chart_type and self._llm is not None:
                self._emit_progress(turn, f"检测到图表生成意图（{chart_type}），自动生成...", "chart")
                try:
                    # 用 LLM 从用户自然语言中提取图表数据
                    zh = self._is_zh()
                    extract_prompt = (
                        f"用户请求生成一个{chart_type}图表。请从以下描述中提取图表数据，"
                        f"输出 JSON 对象，字段名根据图表类型而定：\n"
                        f"flowchart → nodes(list of {{id,label,shape}}), edges(list of {{from,to,label}})\n"
                        f"sequence → actors(list), messages(list of {{from,to,text,type}})\n"
                        f"pie → title, items(list of {{label,value}})\n"
                        f"gantt → title, tasks(list of {{name,start,end}})\n"
                        f"timeline → title, events(list of {{date,description}})\n"
                        f"mindmap → title, children(list of {{name,children}} or string)\n\n"
                        f"用户描述：{text[:800]}\n\n"
                        f"只输出 JSON，不要其他内容："
                    ) if zh else (
                        f"Extract chart data from this description. Output JSON only.\n"
                        f"Chart type: {chart_type}\nDescription: {text[:800]}"
                    )
                    extract_result = await self._llm.chat_completion(
                        messages=[{"role": "user", "content": extract_prompt}],
                        model=turn.model,
                        temperature=0.2,
                        max_tokens=800,
                        tools=None,
                        use_cache=False,
                        response_format={"type": "json_object"},
                    )
                    data_text = (extract_result.get("text") or "{}").strip()
                    key_update = {}
                    if data_text:
                        import json as _json
                        try:
                            cleaned = data_text
                            if cleaned.startswith("```"):
                                cleaned = cleaned.split("```", 2)[1]
                                if cleaned.startswith("json"):
                                    cleaned = cleaned[4:]
                            data = _json.loads(cleaned)
                            if isinstance(data, dict):
                                key_update = data
                        except Exception:
                            pass
                    gen = get_chart_generator()
                    turn.result = gen.generate_mermaid(chart_type, key_update)
                    turn.meta["auto_chart_triggered"] = True
                    turn.record_success(turn.result, 0)
                    self.publish("turn_completed", turn=turn)
                    return True
                except Exception as exc:
                    logger.debug("auto chart failed, falling back: %s", exc)

        return False

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
            # 修复：移除 key.startswith(cmd) 分支，避免 "/h" 匹配 "/help" 的歧义
            for key in self._SLASH_COMMANDS:
                if cmd.startswith(key):
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

        # ---- Round 6: new feature commands (handled directly) ----
        if skill_id == "_rewrite":
            await self._handle_rewrite(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_deep_research":
            await self._handle_deep_research(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_batch":
            await self._handle_batch(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_model_compare":
            await self._handle_model_compare(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_eval":
            await self._handle_eval(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True

        # ---- Round 7: new feature commands ----
        if skill_id == "_email":
            await self._handle_email(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_calendar":
            await self._handle_calendar(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_db":
            await self._handle_db(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_mcp":
            await self._handle_mcp(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_openapi":
            await self._handle_openapi(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_agent_mesh":
            await self._handle_agent_mesh(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_workflow":
            await self._handle_workflow(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_chart":
            await self._handle_chart(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_branch":
            await self._handle_branch(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_branch_switch":
            await self._handle_branch_switch(turn, args_text)
            self.publish("turn_completed", turn=turn)
            return True
        if skill_id == "_branch_list":
            await self._handle_branch_list(turn, args_text)
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
        try:
            if turn.input_text and turn.input_text.strip().startswith("/"):
                if await self._handle_slash_command(turn):
                    return
        except Exception as exc:  # noqa: BLE001
            logger.error("slash command handler failed: %s", exc, exc_info=True)
            turn.record_failure(str(exc))
            self.publish("turn_completed", turn=turn)
            return

        # 自然语言→skill 直通：检测"拉取/添加/刷新模型"类自然语言意图，
        # 直接调度 model_manage skill，绕过弱模型的 tool-calling 限制。
        # 这让用户说"商汤有新免费模型，拉取一下"就能触发，无需 /添加模型。
        try:
            if turn.input_text and self._skills is not None:
                if await self._maybe_direct_skill_dispatch(turn):
                    return
        except Exception as exc:  # noqa: BLE001
            logger.error("direct skill dispatch failed: %s", exc, exc_info=True)
            turn.record_failure(str(exc))
            self.publish("turn_completed", turn=turn)
            return

        # 傻瓜化：检测自然语言中的批量任务/模型对比/图表生成意图，自动调用
        # /batch、/compare、/chart 等处理器。用户无需知道斜杠命令。
        try:
            if turn.input_text and self._llm is not None:
                if await self._maybe_auto_special_task(turn):
                    return
        except Exception as exc:  # noqa: BLE001
            logger.error("auto special task failed: %s", exc, exc_info=True)
            turn.record_failure(str(exc))
            self.publish("turn_completed", turn=turn)
            return

        # Auto-detect language from user input
        if turn.input_text:
            from i18n import detect_language, get_language, set_language
            detected_lang = detect_language(turn.input_text)
            current_lang = get_language()
            if detected_lang != current_lang:
                set_language(detected_lang)
                logger.info("Auto-detected language: %s from user input", detected_lang)
                # Persist language preference to config (offload disk I/O)
                await asyncio.to_thread(self._persist_language, detected_lang)

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
                # 通知网关 turn 已失败（带 error），让用户收到超时提示而非一直等。
                # 之前只 record_failure 不 publish，导致网关永远收不到
                # turn_completed，心跳不停、用户干等。
                self.publish("turn_completed", turn=turn, session_id=session_id)
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
        from core.context import current_turn_var
        token = current_turn_var.set(turn)
        try:
            await self._run_turn_inner(turn)
        finally:
            current_turn_var.reset(token)

    async def _run_turn_inner(self, turn: TurnContext) -> None:
        if turn.input_text is None:
            raise RuntimeError("turn.input_text cannot be None")

        if self._llm is None:
            turn.record_failure("LLM provider not bound")
            self.publish("turn_completed", turn=turn)
            return

        if turn.model is None:
            raise RuntimeError("Model must be set before execution")

        # Gap 3+4 修复：输入安全扫描 — PII检测 + 有害内容 + 注入防御
        # 深度审计 P2-5 修复：之前只存 safety_report 到 meta, 未真正脱敏 / 未注入告警 hint
        safety_report = scan_input(turn.input_text)
        turn.meta["safety_report"] = safety_report

        if safety_report.pii_found:
            # 保留原始输入不替换 —— PII 脱敏只在日志层做（log_sanitizer）。
            # 用户可能主动分享 API key 给 Agent 使用，替换输入文本会导致
            # LLM 拿到的 key 变成 sk-***，从而 API 调用失败。
            turn.meta["safety_original_input"] = turn.input_text  # 保留原始用于审计日志
            logger.info("safety: detected %d PII types in input: %s",
                        len(safety_report.pii_found),
                        [p["type"] for p in safety_report.pii_found])

        if safety_report.injection_found:
            logger.warning("safety: detected prompt injection attempt: %s",
                           [i["type"] for i in safety_report.injection_found])

        if safety_report.harmful_found:
            logger.warning("safety: detected harmful content: %s",
                           [h["type"] for h in safety_report.harmful_found])

        # Gap 10 修复：跨轮次任务状态追踪
        # 从 dialog_summarizer 获取当前活跃任务，注入到 turn.meta
        try:
            summarizer = get_dialog_summarizer()
            active_task = summarizer.get_active_task(turn.session_id)
            if active_task and active_task.status == "in_progress":
                turn.meta["active_task"] = {
                    "name": active_task.name,
                    "steps": active_task.steps,
                    "created_at": active_task.created_at,
                }
                logger.debug("task_state: active task '%s' with %d steps",
                           active_task.name, len(active_task.steps))
        except Exception as exc:
            logger.debug("task_state: get_active_task skipped: %s", exc)

        messages = await self._prepare_messages(turn)
        tools = self._prepare_tools(turn)

        # --- Auto-enable OS mode when router detects system operation needs ---
        # If the router classified this turn as needing system access,
        # automatically enable OS mode (password is optional if not configured)
        if not self._os_mode_enabled and turn.meta.get("needs_system"):
            os_enabled = await self._enable_os_mode(turn, "")
            if os_enabled:
                self._os_mode_enabled = True
                logger.info("OS mode auto-enabled based on router intent classification")
                # Re-prepare tools now that OS mode is enabled
                tools = self._prepare_tools(turn)

        # 深度审计 P2-5 修复：把安全提示注入 system 消息, 让 LLM 真正看到注入防御指令
        safety_report = turn.meta.get("safety_report")
        if safety_report and (safety_report.injection_found or safety_report.harmful_found or safety_report.pii_found):
            hint = safety_report.to_context_hint(zh=self._is_zh())
            if hint:
                # 注入到第一条 system 消息末尾, 或新增一条 system 消息
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = self._append_to_content(messages[0]["content"], hint)
                else:
                    messages.insert(0, {"role": "system", "content": hint})
                logger.info("safety: injected context hint (%d injection, %d harmful, %d PII)",
                            len(safety_report.injection_found),
                            len(safety_report.harmful_found),
                            len(safety_report.pii_found))

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
            self._emit_progress(turn, "正在分析任务并规划工具链...", "planning")
            if complexity >= TOOL_CHAIN_PLANNING_MIN_COMPLEXITY:
                await self._plan_tool_chain(messages, turn, tools)
            # multi-agent publishes turn_completed itself on success
            self._emit_progress(turn, "正在启动多智能体协作...", "multi_agent")
            multi_agent_done = await self._multi_agent_phase(messages, turn)
        elif complexity >= COMPLEX_COMPLEXITY_THRESHOLD:
            # Complex level: think + reflect in ONE call (performance optimization)
            self._emit_progress(turn, "正在思考分析...", "thinking")
            await self._think_phase(messages, turn, include_reflection=True)
        elif complexity >= THINK_MIN_COMPLEXITY:
            # Simple level: lightweight thinking for better reasoning
            await self._think_phase(messages, turn)
        # else: trivial — skip thinking entirely for speed

        # If multi-agent handled the turn, it already published turn_completed.
        # Skip the rest to avoid double-publishing and wasted work.
        if multi_agent_done:
            return

        # --- Step 2.3: Auto deep research for research-heavy complex tasks ---
        # Round 8: 之前 DEEP_RESEARCH_MIN_COMPLEXITY 定义了但从未使用，
        # 深度研究只能通过 /deep 斜杠命令手动触发。
        # 现在：complexity >= 0.7 且任务为研究型时自动触发。
        if complexity >= DEEP_RESEARCH_MIN_COMPLEXITY and self._is_research_task(turn.input_text):
            self._emit_progress(turn, "检测到研究型任务，正在启动深度研究...", "deep_research")
            deep_done = await self._auto_deep_research(turn)
            if deep_done:
                return  # deep research published turn_completed itself

        # Context compression (always, but cheap when not needed)
        await self._compress_context(messages, turn)

        # --- Step 2.5: 本地服务商解析（先查本地再搜） ---
        # 用户提到服务商名称+key 时，先查本地 KNOWN_PROVIDERS 注册表，
        # 找不到再用 API key 探测，全失败才走 web_search。
        await self._maybe_resolve_provider_locally(messages, turn)

        # --- Step 2.6: Auto web-search fallback for models without tool calling ---
        # If the model doesn't support function calling, try to detect
        # search intent and inject results before the main LLM call.
        await self._auto_web_search_if_needed(messages, turn)

        # --- Step 3: Tool-call loop ---
        # 只对复杂任务发进度，简单任务靠心跳兜底
        if complexity >= COMPLEX_COMPLEXITY_THRESHOLD:
            self._emit_progress(turn, "正在执行任务...", "tool_loop")
        await self._tool_loop(messages, turn, tools)

        # --- Step 4: Post-execution quality improvements by tier ---
        # For complex+: combine self-verification and final polish into one
        # LLM call when possible (saves one round-trip).
        result_length = len(turn.result) if turn.result else 0
        if complexity >= FINAL_POLISH_MIN_COMPLEXITY and turn.result and not turn.error:
            self._emit_progress(turn, "正在验证和优化结果...", "verification")
            await self._verify_and_polish(messages, turn, complexity)
            # Skip objective_verify for short answers (< 300 chars) — low error probability
            if result_length >= 300:
                await self._objective_verify(turn)
        elif complexity >= SELF_VERIFY_MIN_COMPLEXITY and turn.result and not turn.error:
            # simple tier: light self-verification only
            self._emit_progress(turn, "正在验证结果...", "verification")
            await self._self_verify(messages, turn, complexity)

        if complexity >= FINAL_POLISH_MIN_COMPLEXITY and turn.result:
            # Skip post_reflect for short answers (< 400 chars) — minimal learning value
            # Post-execution reflection: learn from this turn (complex and above)
            if result_length >= 400:
                await self._post_reflect(turn)

        # Auto-extract entities (offload SQLite writes to worker thread)
        await asyncio.to_thread(self._extract_entities, turn)

        # === Clean up result ===
        # Filter out XML tool-call tags that weak models may emit
        # (e.g. <invoke name="web_search">...</invoke>)
        if turn.result:
            turn.result = self._sanitize_model_output(turn.result)

        # === Intelligent features integration ===
        # 关键修复：之前 6 步后处理任意一步抛异常都会让 turn_completed 永不发布,
        # 用户得不到回复 + 会话历史断裂 + 自演化统计失真。
        # 修复：每步独立 try/except, 异常只记日志不阻断主流程。
        # turn_completed 必须发出 (它是用户拿到回复的唯一信号)。
        async def _safe_step(name, coro):
            try:
                return await coro
            except Exception as exc:
                logger.warning("post-process step '%s' failed (non-fatal): %s", name, exc)
                return None

        def _safe_step_sync(name, fn):
            try:
                return fn()
            except Exception as exc:
                logger.warning("post-process step '%s' failed (non-fatal): %s", name, exc)
                return None

        # 1. Record user preferences and patterns
        await _safe_step("record_intelligence", self._record_intelligence(turn))

        # 2. Generate proactive suggestions (inject into result if enabled)
        suggestions = await _safe_step("generate_suggestions", self._generate_suggestions(turn))
        if suggestions and self._should_show_suggestions():
            suggestion_text = self._suggestions.format_suggestions_for_display(suggestions)
            turn.result = turn.result + suggestion_text

        # 3. Metacognition — analyze response quality
        await _safe_step("analyze_response_quality", self._analyze_response_quality(turn))

        # 4. Round 6: Multi-turn proactive planning — predict next steps
        await _safe_step("proactive_plan", self._proactive_plan(turn))

        # 5. Round 7: Conversation branch auto-tracking
        _safe_step_sync("record_conversation_branch", lambda: self._record_conversation_branch(turn))

        # 6. Round 7 修复: Task state — 检测并更新活跃任务
        await _safe_step("update_task_state", self._update_task_state(turn))

        # 7. Round 8: Task scheduler — 异步跟进任务
        # 对 expert/complex 任务，如果 LLM 提到"稍后做"或"待跟进"，调度一个延迟检查
        # 避免用户问后续状态时需要重新开始整个流程
        if complexity >= COMPLEX_COMPLEXITY_THRESHOLD and turn.result:
            _safe_step_sync(
                "schedule_followup_check",
                lambda: self._maybe_schedule_followup(turn),
            )

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

    @staticmethod
    def _append_to_content(content: Any, suffix: str) -> Any:
        """Append text to a message content field, compatible with both
        str and list (vision/multimodal) content formats.

        修复：之前直接 content + "\n\n" + hint 假设 content 是 str,
        但 OpenAI/Anthropic 多模态格式 content 是 list (如
        [{"type":"text","text":"..."},{"type":"image_url",...}]),
        str + list 抛 TypeError。
        """
        if isinstance(content, str):
            return content + "\n\n" + suffix
        if isinstance(content, list):
            # 多模态：追加一个 text 块
            return content + [{"type": "text", "text": suffix}]
        # content 为 None 或其他类型：直接返回 suffix 作为字符串
        return suffix

    @staticmethod
    def _prepend_to_content(content: Any, prefix: str) -> Any:
        """Prepend text to a message content field (compatible with str/list)."""
        if isinstance(content, str):
            return prefix + "\n\n" + content
        if isinstance(content, list):
            # 多模态：在开头插入一个 text 块
            return [{"type": "text", "text": prefix}] + content
        return prefix

    def _sanitize_model_output(self, text: str) -> str:
        """Remove XML tool-call tags that weak models may emit as text.

        Some models (especially flash/lite variants) output tool-call XML
        like <invoke name="web_search">...</invoke> or <tool_call ...>...
        directly in their text response instead of using the proper API.
        This strips those tags so users never see raw XML.
        """
        import re
        # Remove <invoke ...>...</invoke> blocks
        text = re.sub(
            r'<invoke\s+name="[^"]*">.*?</invoke>',
            '',
            text,
            flags=re.DOTALL,
        )
        # Remove <parameter ...>...</parameter> blocks
        text = re.sub(
            r'<parameter\s+name="[^"]*">.*?</parameter>',
            '',
            text,
            flags=re.DOTALL,
        )
        # Remove standalone <invoke ...> tags (unclosed)
        text = re.sub(r'<invoke\s+name="[^"]*"[^>]*/?\s*>', '', text)
        # Remove <tool_call ...>...</tool_call > blocks
        text = re.sub(
            r'<tool_call[^>]*>.*?</tool_call\s*>',
            '',
            text,
            flags=re.DOTALL,
        )
        # Remove <function_call ...>...</function_call> blocks
        text = re.sub(
            r'<function_call[^>]*>.*?</function_call\s*>',
            '',
            text,
            flags=re.DOTALL,
        )
        # Remove self-closing tool tags like <web_search query="..."/>
        # or <system_run command="..."/> that weak models emit when they
        # can't use function calling. These may have been parsed & executed
        # by _parse_xml_tool_tags, but the raw tags must not leak to users.
        text = self._strip_executed_xml_tags(text)
        # Clean up excessive blank lines left behind
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # 已知工具名集合 — 用于校验解析出的标签名是否合法
    _XML_TOOL_NAMES = frozenset({
        "web_search", "python_execute", "calc", "send_message",
        "system_run", "settings", "model_manage", "now", "email",
        "calendar", "database", "mcp", "openapi", "workflow",
        "chart", "branch", "branch_switch", "branch_list",
    })

    def _parse_xml_tool_tags(self, text: str) -> List[Dict[str, Any]]:
        """从 LLM 输出文本中解析自闭合 XML 工具标签，转为 tool_calls 结构。

        当模型不支持 OpenAI function calling（如 sensenova flash-lite）时，
        LLM 可能输出 <web_search query="..."/> 这类标签来表达工具调用意图。
        本方法把这些标签解析成标准 tool_calls 结构，让 _execute_tool_calls 能执行。

        支持的格式（自闭合，属性即参数）：
            <web_search query="dmxapi API 文档" />
            <system_run command="curl -s https://api.dmxapi.cn/v1/models" />
            <calc expr="1+1" />

        也支持成对标签：
            <web_search query="..."></web_search>

        返回 [] 表示没有可解析的工具标签。
        """
        import re as _re
        import json as _json

        if not text or "<" not in text:
            return []

        calls: List[Dict[str, Any]] = []
        # 匹配 <tool_name attr1="v1" attr2='v2' />  或  <tool_name ...></tool_name>
        # 限制 tool_name 只能是已知工具名，避免误解析普通 XML/HTML
        pattern = _re.compile(
            r'<(?P<name>[a-z_]+)(?P<attrs>(?:\s+[a-zA-Z_]\w*\s*=\s*(?:"[^"]*"|\'[^\']*\'))+)\s*/?>',
            _re.IGNORECASE,
        )
        attr_pattern = _re.compile(
            r'(?P<key>[a-zA-Z_]\w*)\s*=\s*(?P<val>"(?P<dval>[^"]*)"|\'(?P<sval>[^\']*)\')',
        )

        for m in pattern.finditer(text):
            name = m.group("name").lower()
            if name not in self._XML_TOOL_NAMES:
                continue
            attrs_str = m.group("attrs")
            args: Dict[str, Any] = {}
            for am in attr_pattern.finditer(attrs_str):
                key = am.group("key")
                # 优先双引号值，否则单引号值
                val = am.group("dval") if am.group("dval") is not None else am.group("sval")
                args[key] = val

            if not args:
                continue

            # 参数名映射：LLM 在 XML 标签里用的参数名可能和技能 schema 定义的不一致。
            # 例如 web_search 的 schema 定义 required=["input"]，但 LLM 输出
            # <web_search query="..."/> 用 query。这里做统一映射。
            _XML_PARAM_MAP = {
                "web_search": {"query": "input", "q": "input"},
                "calc": {"expr": "input", "expression": "input"},
            }
            if name in _XML_PARAM_MAP:
                for xml_key, mapped_key in _XML_PARAM_MAP[name].items():
                    if xml_key in args and mapped_key not in args:
                        args[mapped_key] = args.pop(xml_key)

            # 兼容 _execute_tool_calls 期望的字段格式
            calls.append({
                "id": f"xml_{len(calls)}_{name}",
                "name": name,
                "args": args,
            })

        return calls

    def _strip_executed_xml_tags(self, text: str) -> str:
        """从输出文本中移除已解析执行过的 XML 工具标签，避免泄漏给用户。

        与 _sanitize_model_output 不同，本方法只移除已知工具名的自闭合标签，
        保留普通文本内容。在 _parse_xml_tool_tags 成功解析后调用。
        """
        import re as _re
        if not text or "<" not in text:
            return text
        # 构建正则：<(?:web_search|system_run|calc|...)\s+.../>
        names_alt = "|".join(_re.escape(n) for n in self._XML_TOOL_NAMES)
        # 移除自闭合标签 <tool_name .../>
        text = _re.sub(
            rf'<(?:{names_alt})(?:\s+[a-zA-Z_]\w*\s*=\s*(?:"[^"]*"|\'[^\']*\'))+\s*/?>',
            '',
            text,
            flags=_re.IGNORECASE,
        )
        # 移除成对空标签 <tool_name ...></tool_name>
        text = _re.sub(
            rf'<(?:{names_alt})(?:\s[^>]*)?>\s*</(?:{names_alt})>',
            '',
            text,
            flags=_re.IGNORECASE,
        )
        # 清理多余空行
        text = _re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

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

    async def _prepare_messages(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Prepare message list with memory snippets from long-term memory + KG.

        Memory is injected as a dedicated assistant-style "relevant memory" message
        rather than quietly appended to the user message, so the LLM can reliably
        see it. The router is responsible for putting the system prompt + history
        + user message into ``turn.messages``; we layer memory on top here.
        """
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
                    retrieved = await self._retrieve_memory_for(turn, memory_plugin)
                    if retrieved:
                        memory_snippets = retrieved
                        turn.meta["memory_snippets"] = retrieved
                except Exception as exc:
                    logger.warning("active memory retrieval failed: %s", exc)

        if memory_snippets:
            if self._is_zh():
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

        # Gap 2 修复：指代消解。检测用户输入中的指代词，
        # 从上一轮对话中提取实体信息注入上下文，帮助 LLM 解析指代。
        if self._is_zh():
            referential_words = {"那个", "这个", "它", "他", "她", "它们", "他们", "她们", "其", "该"}
        else:
            referential_words = {"that", "this", "it", "they", "them", "he", "she", "those", "these"}
        input_words = set(re.findall(r"[\w\u4e00-\u9fff]+", turn.input_text.lower()))
        if input_words & referential_words:
            # 从历史中提取上一轮的关键实体
            history = turn.meta.get("history", [])
            if history:
                last_turn = history[-1] if history else {}
                last_input = str(last_turn.get("input", ""))[:200]
                last_reply = str(last_turn.get("reply", ""))[:200]
                if last_input or last_reply:
                    zh = self._is_zh()
                    if zh:
                        ref_hint = (
                            "【指代解析提示】当前用户输入包含指代词（如'那个/这个/它'）。"
                            "上一轮对话的上下文：\n"
                            f"上一轮用户输入：{last_input}\n"
                            f"上一轮你的回复：{last_reply[:200]}\n"
                            "请根据这段上下文解析用户当前指代的具体对象，不要再反问用户。"
                        )
                    else:
                        ref_hint = (
                            "[Reference Resolution] The current input contains referential words. "
                            "Previous turn context:\n"
                            f"Previous user input: {last_input}\n"
                            f"Previous your reply: {last_reply[:200]}\n"
                            "Resolve what the user is referring to based on this context."
                        )
                    ref_block = {"role": "assistant", "content": ref_hint}
                    if messages and messages[-1].get("role") == "user":
                        messages.insert(len(messages) - 1, ref_block)
                    else:
                        messages.append(ref_block)

        # Procedural 记忆注入：MemoryPlugin 在 _on_user_message 里查 procedural 技能，
        # 命中后放入 turn.meta["procedural_skill"]。这里把它作为参考注入 messages，
        # 让 LLM 能复用之前学到的解决方案模式，避免重复探索。
        procedural_body = turn.meta.get("procedural_skill")
        if procedural_body:
            if self._is_zh():
                proc_header = "【学到的技能】（从历史对话中自动提炼的可复用方案）\n以下是我之前解决类似问题时学到的有效步骤，请参考但根据当前情况灵活调整：\n\n"
            else:
                proc_header = "[Learned Skill] (reusable solution pattern distilled from past conversations)\nThe following is an effective approach I learned from similar past problems. Use it as reference but adapt to the current situation:\n\n"
            proc_block = {"role": "assistant", "content": proc_header + procedural_body[:2000]}
            if messages and messages[-1].get("role") == "user":
                messages.insert(len(messages) - 1, proc_block)
            else:
                messages.append(proc_block)

        # Gap 8 修复：跨会话知识迁移。将用户偏好、常用技能、关注话题
        # 等持久化画像注入上下文，让 LLM 跨会话保持一致的个性化体验。
        try:
            profile = self._get_profile()
            profile_summary = profile.get_profile_summary()
            if profile_summary and any(profile_summary.values()):
                parts = []
                prefs = profile_summary.get("preferences", {})
                if prefs:
                    pref_items = []
                    for k, v in list(prefs.items())[:8]:
                        pref_items.append(f"  - {k}: {v}")
                    if pref_items:
                        parts.append("偏好:\n" + "\n".join(pref_items))
                top_skills = profile_summary.get("top_skills", [])
                if top_skills:
                    skill_str = ", ".join(f"{s[0]}({s[1]}次)" for s in top_skills[:5])
                    parts.append(f"常用技能: {skill_str}")
                top_topics = profile_summary.get("top_topics", [])
                if top_topics:
                    topic_str = ", ".join(t[0] for t in top_topics[:5])
                    parts.append(f"关注话题: {topic_str}")
                active_hours = profile_summary.get("active_hours", [])
                if active_hours:
                    parts.append(f"活跃时段: {active_hours} 点")
                if parts:
                    if self._is_zh():
                        profile_header = (
                            "【用户画像】（跨会话持久化的个性化信息）\n"
                            "以下是系统从历史对话中学习到的用户偏好和习惯，"
                            "请在回复时参考但不要刻意提及：\n\n"
                        )
                    else:
                        profile_header = (
                            "[User Profile] (cross-session personalized information)\n"
                            "The following are user preferences and patterns learned from "
                            "past conversations. Use them naturally in your response "
                            "but don't mention them explicitly:\n\n"
                        )
                    profile_block = {"role": "assistant", "content": profile_header + "\n".join(parts)}
                    if messages and messages[-1].get("role") == "user":
                        messages.insert(len(messages) - 1, profile_block)
                    else:
                        messages.append(profile_block)
        except Exception as exc:
            logger.debug("user profile injection skipped: %s", exc)

        # Gap 10 修复：注入活跃任务状态，让 LLM 知道"上次做到哪了"
        # 但如果用户问的是新问题（如"你能做什么"、"介绍一下"等），
        # 不要注入 active_task，让 LLM 回答新问题而不是继续之前的任务。
        active_task = turn.meta.get("active_task")
        if active_task:
            input_lower = turn.input_text.lower()
            new_question_signals = [
                "你能做什么", "你会什么", "你现在能做什么", "你有什么能力",
                "介绍一下", "说明一下", "展示一下", "演示一下",
                "新任务", "另一个任务", "别的任务", "新问题",
                "换一个", "重新开始", "从头开始", "reset", "clear",
                "能力", "功能", "帮助", "help",
            ]
            if any(signal in input_lower for signal in new_question_signals):
                logger.debug("skipping active_task injection: user asked new question")
            else:
                steps_text = ""
                for i, step in enumerate(active_task.get("steps", [])):
                    status_icon = {"pending": "⬜", "in_progress": "🔄", "done": "✅", "failed": "❌"}.get(
                        step.get("status", "pending"), "⬜")
                    steps_text += f"\n  {status_icon} Step {i+1}: {step.get('step', '')}"
                    if step.get("result"):
                        steps_text += f" — {step['result'][:80]}"
                if self._is_zh():
                    task_header = (
                        f"【继续任务】当前有一个进行中的任务：{active_task.get('name', '')}\n"
                        f"进度：{steps_text}\n"
                        "请继续执行此任务，从上次中断的地方接着做。"
                    )
                else:
                    task_header = (
                        f"[Continue Task] Active task: {active_task.get('name', '')}\n"
                        f"Progress: {steps_text}\n"
                        "Continue from where you left off."
                    )
                task_block = {"role": "assistant", "content": task_header}
                if messages and messages[-1].get("role") == "user":
                    messages.insert(len(messages) - 1, task_block)
                else:
                    messages.append(task_block)

        # 注入对话摘要：长对话超过 N 轮后，早期上下文会被 router 截断。
        # 对话摘要提供了早期对话的回顾，让 agent 不会"失忆"。
        try:
            summarizer = self._get_dialog_summarizer()
            lang = "zh" if self._is_zh() else "en"
            summary_text = summarizer.format_summary_for_context(turn.session_id, lang)
            if summary_text:
                summary_block = {"role": "assistant", "content": summary_text}
                if messages and messages[-1].get("role") == "user":
                    messages.insert(len(messages) - 1, summary_block)
                else:
                    messages.append(summary_block)
                turn.meta["dialog_summary_injected"] = True
        except Exception:
            pass

        # Gap 5 修复：注入上一轮生成的主动规划，让 LLM 预判用户下一步
        proactive_plan = self._get_proactive_plan(turn.session_id)
        if proactive_plan:
            plan_text = "\n".join(f"- {p}" for p in proactive_plan)
            if self._is_zh():
                plan_header = (
                    "【下一步预判】（系统预测用户接下来可能追问的问题）\n"
                    "请在心里准备好这些问题的答案，但不要主动输出：\n"
                )
            else:
                plan_header = (
                    "[Next-Step Prediction] (system predicted follow-up questions)\n"
                    "Prepare answers for these mentally, but don't output them proactively:\n"
                )
            plan_block = {"role": "assistant", "content": plan_header + plan_text}
            if messages and messages[-1].get("role") == "user":
                messages.insert(len(messages) - 1, plan_block)
            else:
                messages.append(plan_block)
            turn.meta["proactive_plan_injected"] = True

        # Inject Chain-of-Thought reasoning for complex tasks
        await self._maybe_inject_cot(messages, turn)

        # 注入持久化的自我改进建议：SelfImprover 把失败→改进写入 DB，
        # 这里每轮读最近 3 条作为持久化行为指导注入 system prompt。
        # 之前只写不读 → self-improvement 是开环的，学到的改进从不改变后续行为。
        if self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver:
            try:
                active_improvements = self.ctx.self_improver.get_active_improvements(limit=3)
                if active_improvements:
                    imp_text = "\n".join(f"- {s}" for s in active_improvements)
                    imp_msg = {
                        "role": "system",
                        "content": f"[行为改进指南]（基于历史失败自动学习）\n{imp_text}",
                    }
                    # 插到第一个 system 消息之后，作为持久化指令
                    if messages and messages[0].get("role") == "system":
                        messages.insert(1, imp_msg)
                    else:
                        messages.insert(0, imp_msg)
                    turn.meta["active_improvements"] = len(active_improvements)
            except Exception as exc:
                logger.debug("加载持久化改进失败: %s", exc)

        # 注入重启感知：如果刚重启过，告诉 LLM 已成功重启
        if self.ctx and getattr(self.ctx, "recent_restart", 0):
            import time as _t
            elapsed = _t.time() - self.ctx.recent_restart
            if elapsed < 120:  # 2 分钟内
                restart_note = (
                    "[系统提示：One-Agent 刚刚已成功重启，新版本已生效。"
                    "如果用户问是否已重启，请确认已重启完成。]"
                )
                # 插入到消息列表开头（系统提示之后）
                for i, m in enumerate(messages):
                    if m.get("role") == "system":
                        messages.insert(i + 1, {"role": "system", "content": restart_note})
                        break
                else:
                    messages.insert(0, {"role": "system", "content": restart_note})

        # 注入回复风格：StyleAdapter 根据用户历史反馈/画像自动调整回复风格
        # （简洁/详细、正式/友好、emoji、代码详尽度等）。之前 getter 定义了却
        # 从未被调用 → 风格个性化能力完全是死代码。这里激活它，让 agent 自动
        # 适应用户的沟通偏好，无需用户手动配置。
        try:
            style_adapter = self._get_style_adapter()
            lang = "zh" if self._is_zh() else "en"
            style_snippet = style_adapter.generate_system_prompt_snippet(lang=lang)
            if style_snippet:
                style_msg = {"role": "system", "content": style_snippet}
                # 插到第一个 system 消息之后，作为持久化风格指令
                if messages and messages[0].get("role") == "system":
                    messages.insert(1, style_msg)
                else:
                    messages.insert(0, style_msg)
                turn.meta["style_adapter_injected"] = True
        except Exception as exc:
            logger.debug("style adapter injection skipped: %s", exc)

        return messages

    async def _retrieve_memory_for(self, turn: TurnContext, memory_plugin) -> str:
        """Query long-term memory + knowledge graph for relevant snippets.

        All SQLite/embedding operations are offloaded to a worker thread
        via ``asyncio.to_thread`` so the event loop is not blocked during
        FTS5 queries, SentenceTransformer inference, or vector scans.
        """
        hits: List[str] = []
        query = turn.input_text or ""
        if not query.strip():
            return ""

        # 1) Long-term FTS5 / hybrid search
        long_term = getattr(memory_plugin, "_long", None)
        if long_term is not None:
            try:
                fts_hits = await asyncio.to_thread(long_term.search, query, 5) or []
                for h in fts_hits:
                    content = h.get("content", "")
                    source = h.get("source", "memory")
                    if content and len(content) > 5:
                        hits.append(f"- [记忆/{source}] {content[:300]}")
            except Exception as exc:
                logger.debug("long-term memory search failed: %s", exc)

        # 2) Embedding semantic search — embed() loads the model on first call
        # and runs CPU-bound inference, so it MUST be offloaded.
        embeddings = getattr(memory_plugin, "_embeddings", None)
        if embeddings is not None:
            try:
                query_vec = await asyncio.to_thread(embeddings.embed, query)
                if query_vec is not None:
                    sem = await asyncio.to_thread(embeddings.search, query_vec, 5) or []
                    seen_contents = {h.split("] ", 1)[1][:40] for h in hits}
                    # Batch-fetch all semantic hits in one query (was N+1).
                    sem_ids = [mid for mid, _score in sem]
                    entries_map: Dict[str, Any] = {}
                    if long_term and sem_ids:
                        entries_map = await asyncio.to_thread(long_term.get_by_ids, sem_ids)
                    for memory_id, _score in sem:
                        entry = entries_map.get(str(memory_id))
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
                kg_hits = await asyncio.to_thread(kg.search, query, 5) or []
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

        if self._is_zh():
            header = "以下是从我的记忆系统中检索到的、与当前问题最相关的内容 — 请优先参考：\n"
        else:
            header = "Retrieved from memory — most relevant to current question:\n"
        return header + "\n".join(hits[:5])

    def _prepare_tools(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Pick relevant skills and prepare tool schemas.

        Core tools (web_search, python_execute, calc) are always available
        regardless of keyword relevance, because they are fundamental
        capabilities that should never be missing from the LLM's tool set.

        When OS mode is enabled (via /os-on) or the router detects system
        access needs, system_run is also auto-added so the LLM can directly
        call it for system operations.

        When the router detects settings intent (needs_settings), the settings
        tool is auto-added so the LLM can modify/view Agent configuration
        via natural language commands.
        """

        tools: List[Dict[str, Any]] = []
        if self._skills is not None:
            chosen = self._skills.pick_relevant(turn.input_text, limit=6)
            # Core tools — always available regardless of keyword match
            for core_id in ("web_search", "python_execute", "calc", "send_message"):
                core = self._skills.get(core_id)
                if core and core not in chosen:
                    chosen.insert(0, core)
            # OS mode: explicit enable OR auto-detected by router
            needs_sys = self._os_mode_enabled or turn.meta.get("needs_system_access")
            if needs_sys:
                system_run = self._skills.get("system_run")
                if system_run and system_run not in chosen:
                    chosen.append(system_run)
            # Settings: auto-add when router detects settings intent
            if turn.meta.get("needs_settings"):
                settings = self._skills.get("settings")
                if settings and settings not in chosen:
                    chosen.append(settings)
            turn.skills = [s.id for s in chosen]
            tools = [s.schema for s in chosen]
        else:
            turn.skills = []
        return tools

    async def _think_phase(self, messages: List[Dict[str, Any]], turn: TurnContext, include_reflection: bool = False) -> None:
        """Execute structured thinking phase (Chain-of-Thought style planning).

        This is the thinking backbone of One-Agent. Instead of the previous
        "think 2-4 sentences then act", we ask the LLM to produce a real
        7-step plan. The plan is appended to ``messages`` as a structured
        assistant response, so every subsequent tool-loop call can see the
        plan and is more likely to follow it instead of drifting into
        superficial chatter.

        When include_reflection=True, combines thinking + reflection in ONE
        LLM call to save latency. Uses lightweight model for auxiliary calls.

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

        # Use lightweight model for thinking to reduce latency
        model = self._lightweight_model(turn)

        # Build the thinking prompt.  We attach it as an additional user
        # message so the model has access to the full conversation history
        # (including memory) while planning.
        memory_snippets = turn.meta.get("memory_snippets") or ""
        if self._is_zh():
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

        if include_reflection:
            if self._is_zh():
                plan_prompt += (
                    "\n\n【内部反思 — 在完成上述规划后立即执行】\n\n"
                    "请站在更高的角度审视你刚刚制定的计划，找出潜在问题和改进空间。\n\n"
                    "请思考并回答以下问题：\n"
                    "1. 计划中最大的风险是什么？哪个环节最可能失败？\n"
                    "2. 是否遗漏了用户可能关心的边界情况或细节？\n"
                    "3. 各个步骤之间是否存在依赖关系没考虑到？\n"
                    "4. 如果某个工具调用失败，备用方案是否足够有效？\n"
                    "5. 是否有更高效的路径可以达到相同目标？\n"
                    "6. 最终输出是否真的能满足用户的核心需求？\n\n"
                    "请用简洁的语言总结你的反思结论，并给出具体的改进建议（如果有的话）。\n\n"
                    "输出格式要求：先输出【计划】部分（7步），然后输出【反思】部分（回答上述6个问题）。"
                )
            else:
                plan_prompt += (
                    "\n\n[Internal reflection — do this IMMEDIATELY after the plan]\n\n"
                    "Step back and critically review your plan for potential flaws.\n\n"
                    "Answer these questions:\n"
                    "1. What is the biggest risk in this plan? Which step is most likely to fail?\n"
                    "2. Are there any edge cases or details the user might care about that were missed?\n"
                    "3. Are there dependencies between steps that weren't considered?\n"
                    "4. If a tool call fails, is the fallback sufficient?\n"
                    "5. Is there a more efficient path to the same goal?\n"
                    "6. Will the final output truly address the user's core need?\n\n"
                    "Summarize your reflections and provide specific improvement suggestions if any.\n\n"
                    "Output format: First [PLAN] section (7 steps), then [REFLECTION] section (answers to the 6 questions)."
                )

        thinking_messages = list(messages) + [{"role": "user", "content": plan_prompt}]

        max_tokens = min(turn.token_budget or 2048, 1400 if include_reflection else 900)

        try:
            think_resp = await self._llm.chat_completion(
                messages=thinking_messages,
                model=model,
                max_tokens=max_tokens,
                tools=None,
            )
            thinking_text = (think_resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("think phase skipped: %s", exc)
            thinking_text = ""

        if thinking_text:
            if include_reflection:
                zh = self._is_zh()
                plan_marker = "【反思】" if zh else "[REFLECTION]"
                if plan_marker in thinking_text:
                    plan_part, reflect_part = thinking_text.split(plan_marker, 1)
                    thinking_text = plan_part.strip()
                    reflect_text = reflect_part.strip()
                else:
                    plan_marker2 = "【计划】" if zh else "[PLAN]"
                    if plan_marker2 in thinking_text:
                        parts = thinking_text.split(plan_marker2)
                        thinking_text = parts[-1].strip() if len(parts) > 1 else thinking_text
                        reflect_text = ""
                    else:
                        reflect_text = ""

                turn.meta["thinking"] = thinking_text
                if reflect_text:
                    turn.meta["reflection"] = reflect_text
                    header = "【我的反思与改进】\n" if zh else "[My reflection and improvements]\n"
                    reflect_message = {"role": "assistant", "content": header + reflect_text}
                    messages.append(reflect_message)
                    prompt = (
                        "好。根据你的反思，如果需要调整计划，请立即执行调整后的方案。"
                        if zh
                        else "Good. Based on your reflection, execute with any adjustments needed."
                    )
                    messages.append({"role": "user", "content": prompt})
                    logger.debug("combined think+reflect phase completed (%d chars)", len(thinking_text) + len(reflect_text))
                else:
                    turn.meta["reflection"] = ""
            else:
                turn.meta["thinking"] = thinking_text

            header = "【我的执行计划】\n" if self._is_zh() else "[My execution plan]\n"
            plan_message = {"role": "assistant", "content": header + thinking_text}
            messages.append(plan_message)
            prompt = (
                "好。现在按照上面的计划一步一步执行。"
                if self._is_zh()
                else "Good. Now execute the plan step by step."
            )
            messages.append({"role": "user", "content": prompt})
            logger.debug("think phase completed (%d chars)", len(thinking_text))
        else:
            turn.meta["thinking"] = ""
            if include_reflection:
                turn.meta["reflection"] = ""

    async def _reflect_phase(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Execute reflection phase — critically review the plan before execution.

        For complex tasks (complexity >= 0.5), after creating the initial plan,
        we ask the model to reflect on potential flaws and improvements. This
        meta-cognition step helps catch errors before they lead to wasted
        execution.

        The reflection is injected into the message flow as an assistant message,
        so the subsequent tool loop can benefit from the improved plan.
        """

        plan_text = turn.meta.get("thinking", "")
        if not plan_text:
            logger.debug("reflect phase skipped: no thinking available")
            return

        if self._is_zh():
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
            header = "【我的反思与改进】\n" if self._is_zh() else "[My reflection and improvements]\n"
            reflect_message = {"role": "assistant", "content": header + reflect_text}
            messages.append(reflect_message)
            # Append a user prompt to acknowledge reflection and continue
            prompt = (
                "好。根据你的反思，如果需要调整计划，请立即执行调整后的方案。"
                if self._is_zh()
                else "Good. Based on your reflection, execute with any adjustments needed."
            )
            messages.append({"role": "user", "content": prompt})
            logger.debug("reflect phase completed (%d chars)", len(reflect_text))
        else:
            turn.meta["reflection"] = ""

    async def _multi_agent_phase(self, messages: List[Dict[str, Any]], turn: TurnContext) -> bool:
        """Execute multi-agent pattern for expert-level tasks (complexity >= 0.8).

        Uses AgentMesh with specialized agent roles (researcher, coder, reviewer,
        writer, analyst) to solve complex tasks collaboratively. Each sub-task is
        routed to the most appropriate specialist based on the content.

        Returns True if delegation was successful (no need for further processing),
        False otherwise (fall back to normal flow).
        """

        try:
            mesh = get_agent_mesh(self._llm, self._skills)

            def on_progress(phase: str, desc: str) -> None:
                self._emit_progress(turn, desc, "agent_mesh")

            result = await mesh.solve(
                turn.input_text,
                model=turn.model,
                max_agents=5,
                on_progress=on_progress,
            )

            if result.tasks:
                turn.result = result.final_answer
                turn.meta["delegation_used"] = True
                turn.meta["agent_mesh_used"] = True
                turn.meta["subtask_count"] = len(result.tasks)
                # Estimate tokens from task durations (approximate)
                total_tokens = sum(
                    len(t.result) for t in result.tasks
                ) // 4  # rough estimate: ~4 chars/token
                turn.meta["delegation_total_tokens"] = total_tokens
                turn.record_success(result.final_answer, total_tokens)

                if self.ctx and hasattr(self.ctx, 'memory') and hasattr(self.ctx.memory, '_kg') and self.ctx.memory._kg:
                    full_text = f"{turn.input_text}\n{result.final_answer}"
                    try:
                        count = self.ctx.memory._kg.extract_from_text(full_text, source=turn.session_id)
                        if count > 0:
                            logger.debug("Extracted %d entities from agent-mesh turn %s", count, turn.session_id)
                    except Exception as exc:
                        logger.debug("KG extraction failed in agent-mesh: %s", exc)

                self.publish("turn_completed", turn=turn)
                logger.info("agent-mesh completed (%d agents, %.1fs)",
                            len(result.tasks),
                            result.total_duration)
                return True

        except asyncio.TimeoutError:
            logger.warning("agent-mesh timeout, falling back to normal flow")
        except (KeyError, AttributeError) as exc:
            logger.error("agent-mesh logic error (should be fixed): %s", exc)
        except Exception as exc:
            logger.warning("agent-mesh failed, falling back to normal flow: %s", exc)

        return False

    async def _compress_context(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Compress context if approaching token limit.

        Round 8 修复：使用 ContextCompressor 做重要性评分 + 关键信息提取，
        而不仅仅依赖 LLM 摘要。对 LLM 不可用场景也可用。
        """

        if not (self.ctx and self.ctx.config):
            return

        compression_enabled = self.ctx.config.get("router", {}).get("context_compression", {}).get("enabled", True)
        if not compression_enabled:
            return

        max_tokens = self.ctx.config.get("memory", {}).get("short_term", {}).get("max_tokens", 8000)
        # 性能优化：用 encode('ascii', 'ignore') 统计中文字符
        # 之前: sum(1 for c in content if "\u4e00" <= c <= "\u9fff") 是 O(N*M) Python 循环
        # 现在: C 层编码, 实测快 ~85x (130ms → 1.5ms / 50msgs×1200chars)
        estimated_tokens = 0
        for m in messages:
            content = str(m.get("content", ""))
            if not content:
                continue
            # 非中文字符 = 能被 ASCII 编码的字符 (中文会被 ignore 掉)
            ascii_len = len(content.encode("ascii", "ignore"))
            chinese_chars = len(content) - ascii_len
            estimated_tokens += int(chinese_chars * 0.6 + ascii_len // 4)

        # 缓存估算结果, 避免下游重复计算
        turn.meta["estimated_tokens"] = estimated_tokens

        if estimated_tokens <= max_tokens * 0.8:
            return

        # Round 8：先用 ContextCompressor 做重要性评分 + 关键信息提取
        # 提取出来的 key_points 会注入到摘要 prompt，让 LLM 摘要保留关键事实
        key_points: List[str] = []
        try:
            from core.context_compressor import ContextCompressor
            complexity = getattr(turn, "estimated_complexity", 0.5) or 0.5
            tier = (
                "expert" if complexity >= 0.8 else
                "complex" if complexity >= 0.5 else
                "simple" if complexity >= 0.2 else
                "trivial"
            )
            compressor = ContextCompressor.for_tier(tier)
            _, comp_summary = compressor.compress(messages)
            # comp_summary 已经包含了 topics + key_points，可以直接用
            if comp_summary and "Key information" in comp_summary:
                # 提取要点行
                for line in comp_summary.split("\n"):
                    line = line.strip()
                    if line.startswith("•"):
                        key_points.append(line.lstrip("• ").strip())
            turn.meta["context_compressor_used"] = True
        except Exception as exc:
            logger.debug("context_compressor integration failed: %s", exc)

        # LLM 摘要 (结合 ContextCompressor 提取的要点)
        summary = await self._compress_messages(messages, turn, extra_hints=key_points)
        if summary:
            keep_recent = max(4, len(messages) // 3)
            early = messages[:len(messages) - keep_recent]
            recent = messages[len(messages) - keep_recent:]
            # 保留原始系统提示词（智能路由逻辑、人格设定等关键指令），
            # 否则压缩后会丢失，导致 agent 行为退化。
            original_system = (
                messages[0] if messages and messages[0].get("role") == "system" else None
            )
            # 当 keep_recent >= len(messages) 时（短列表），recent 会包含
            # messages[0]，再手动 append 一次会重复。这里过滤掉原 system。
            if original_system is not None:
                recent = [m for m in recent if m is not original_system]
            messages.clear()
            if original_system is not None:
                messages.append(original_system)
            messages.append({"role": "system", "content": f"[对话历史摘要]\n{summary}"})
            messages.extend(recent)
            turn.meta["context_compressed"] = True
            turn.meta["compressed_messages"] = len(early)

    async def _maybe_resolve_provider_locally(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """先查本地服务商注册表，再探测，最后才搜索。

        当用户输入包含服务商名称和 API key 模式时，按以下顺序查找：
        1. 本地 KNOWN_PROVIDERS 注册表（50+ 已知服务商，毫秒级）
        2. 用 resolver 探测候选 URL（5 秒超时，并行探测）
        3. 全失败 → 不做任何事，让后续 web_search 兜底

        找到后把结果注入 messages，LLM 就不需要再搜索了。
        """
        import re
        from models.resolver import KNOWN_PROVIDERS, lookup, _candidate_hosts

        text = (turn.input_text or "").strip()
        if not text:
            return

        # 检测用户是否提供了提供商名称 + key 模式
        # 匹配: "服务商：dmxapi" / "provider: dmxapi" / "服务商 dmxapi" / "dmxapi key:sk-xxx"
        provider_match = re.search(
            r'(?:服务商|提供商|provider|api\s*服务商)\s*[：:]\s*(\S+)'
            r'|(\S+)\s*(?:key|api\s*key|密钥)\s*[：:]\s*(sk-\S+)',
            text, re.IGNORECASE,
        )
        if not provider_match:
            return

        provider_name = provider_match.group(1) or provider_match.group(2)
        if not provider_name:
            return

        # 清理 provider 名称
        provider_name = re.sub(r'[^\w.-]', '', provider_name.strip().lower())
        if not provider_name or len(provider_name) < 2:
            return

        # 提取 API key
        key_match = re.search(r'(sk-\S+)', text)
        api_key = key_match.group(1) if key_match else ""

        zh = self._is_zh()

        # --- Step 1: 查本地注册表 ---
        url = lookup(provider_name)
        if url:
            msg = (
                f"[系统提示：本地注册表已知服务商 '{provider_name}' → API 地址 {url}。"
                f"请直接使用此地址拉取模型列表，无需搜索。]"
                if zh else
                f"[System: Provider '{provider_name}' found in local registry → {url}. "
                f"Use this URL directly to fetch models, no need to search.]"
            )
            messages.append({"role": "user", "content": msg})
            turn.meta["provider_resolved_locally"] = True
            turn.meta["provider_resolved_via"] = "registry"
            logger.info("provider_resolver: %s found in registry → %s", provider_name, url)
            return

        # --- Step 2: 用 API key 探测候选 URL ---
        if not api_key:
            # 没有 key 无法探测，让 web_search 兜底
            return

        self._emit_progress(turn, f"正在探测 {provider_name} 的 API 地址...", "provider_resolve")
        try:
            from models.resolver import resolve as resolver_resolve
            resolved = await resolver_resolve(provider_name, api_key, timeout=5.0)
        except Exception as exc:
            logger.debug("provider_resolver: probe failed for %s: %s", provider_name, exc)
            return

        if resolved.found:
            msg = (
                f"[系统提示：已自动探测到服务商 '{provider_name}' 的 API 地址：{resolved.base_url} "
                f"（通过{resolved.via}）。请直接使用此地址拉取模型列表，无需搜索。]"
                if zh else
                f"[System: Auto-detected API URL for '{provider_name}': {resolved.base_url} "
                f"(via {resolved.via}). Use this URL directly to fetch models, no need to search.]"
            )
            messages.append({"role": "user", "content": msg})
            turn.meta["provider_resolved_locally"] = True
            turn.meta["provider_resolved_via"] = resolved.via
            logger.info(
                "provider_resolver: %s resolved via %s → %s",
                provider_name, resolved.via, resolved.base_url,
            )
        else:
            # 探测失败，不做任何事，让后续 web_search 兜底
            logger.info(
                "provider_resolver: %s not found in registry or probe, falling through to web_search",
                provider_name,
            )

    def _needs_web_search(self, text: str) -> bool:
        """Heuristic check whether the user's request requires web search.

        Used as a fallback when the model doesn't support tool calling —
        so the agent can still look up real-time info.
        """
        import re
        t = text.lower()
        search_patterns = [
            r"搜索|搜|查找|查一下|查一查|查新|最新|最近|新闻|资讯|头条|热门|热搜|实时|今天|昨天|近日|近期",
            r"web search|search for|look up|find out|what's new|what is new|latest|recent news|current events|breaking",
            r"价格|股价|行情|比分|比赛结果|天气|汇率|价格表|排行榜",
            r"how much|how many|price of|weather|score|result",
        ]
        for pat in search_patterns:
            if re.search(pat, t):
                return True
        if re.search(r"今年|本月|本周|今天|现在|目前|当前|2025|2026", t) and len(t) > 10:
            return True
        return False

    async def _auto_web_search_if_needed(self, messages: List[Dict[str, Any]], turn: TurnContext) -> bool:
        """If the model doesn't support tools, handle the no-tools scenario.

        For models without function-calling support:
        - Always inject a "pure text mode" notice so the model doesn't
          pretend to call tools.
        - If the user query has search intent, auto-run web_search and
          inject the results.

        Returns True if anything was injected into messages.
        """
        if self._llm is None or self._skills is None:
            return False

        model_name = turn.model or ""

        no_tools = False
        if hasattr(self._llm, "model_supports_tools"):
            no_tools = not self._llm.model_supports_tools(model_name)
        elif hasattr(self._llm, "_no_tools_models"):
            bare_model = ""
            if model_name and "/" in model_name:
                bare_model = model_name.split("/", 1)[1]
            elif model_name:
                bare_model = model_name
            no_tools = bare_model in self._llm._no_tools_models

        if not no_tools:
            return False

        import datetime as _dt
        now = _dt.datetime.now()
        current_year = now.year

        needs_search = self._needs_web_search(turn.input_text)

        search_result_text = ""
        search_performed = False

        if needs_search:
            web_search_skill = self._skills.get("web_search")
            if web_search_skill is not None:
                query = turn.input_text.strip()
                if not re.search(r"\d{4}", query) and re.search(
                    r"(今年|本年|本月|这个月|上个月|去年|上月|下个月|下月|最近|近期|\d{1,2}\s*月)", query
                ):
                    query = f"{current_year}年 {query}"

                logger.info("model %s doesn't support tools; auto-searching for: %s",
                            model_name, query[:60])
                try:
                    result = await web_search_skill.run({"input": query})
                    if result and "error" not in str(result).lower() and "无法" not in str(result) and "均无法访问" not in str(result):
                        search_result_text = str(result)[:4000]
                        search_performed = True
                        turn.meta["auto_searched"] = True
                        logger.info("auto-search completed, results injected")

                        # Round 7: 尝试用高级 RAG 增强搜索结果
                        try:
                            enhanced = await self._enhanced_web_search(query, turn)
                            if enhanced and len(enhanced) > 50:
                                search_result_text = enhanced + "\n\n---\n" + search_result_text[:2000]
                                turn.meta["rag_enhanced"] = True
                                logger.debug("auto-search enhanced with RAG (HyDE+rerank)")
                        except Exception:
                            pass

                        # Round 8: 如果 RAG 增强后结果仍然很长 (>6000 chars)，
                        # 用 SubAgent 做信息提取/总结，让 LLM 拿到精炼版结果
                        if search_result_text and len(search_result_text) > 6000:
                            try:
                                from core.sub_agent import SubAgent
                                sub = SubAgent(self._llm, self._skills, "search-summarizer")
                                sub_result = await sub.run(
                                    f"从以下搜索结果中提取与问题最相关的信息（保留关键事实和数据，2000字以内）：\n\n"
                                    f"问题：{turn.input_text[:300]}\n\n"
                                    f"搜索结果：\n{search_result_text[:8000]}",
                                    model=turn.model,
                                    max_iterations=1,
                                )
                                if sub_result.get("result") and not sub_result.get("error"):
                                    search_result_text = sub_result["result"][:4000]
                                    turn.meta["sub_agent_summarized"] = True
                                    logger.debug("auto-search summarized by SubAgent (%d chars)",
                                                 len(search_result_text))
                            except Exception as sub_exc:
                                logger.debug("SubAgent summarization skipped: %s", sub_exc)
                    else:
                        logger.debug("auto-search returned no usable results: %s", str(result)[:200])
                except Exception as exc:
                    logger.warning("auto-web-search failed: %s", exc, exc_info=True)

        mode_notice = (
            "【重要：本对话运行在纯文本模式，你无法调用任何工具（web_search、calc、now、"
            "system_run 等全部不可用）。】\n"
            "【绝对不要假装调用工具、不要描述搜索过程、不要输出\"搜索关键词\"、\"正在搜索\"等"
            "类似演戏的内容。】\n"
        )

        if search_performed and search_result_text:
            search_section = (
                "【系统已自动为你执行了联网搜索，以下是搜索到的最新信息，"
                "请完全基于这些真实信息回答用户问题，不要用旧知识推测，"
                "也不要告诉用户\"我无法联网\"或\"请点击联网搜索按钮\"。】\n\n"
                "━━━━━━━━━━━━ 搜索结果开始 ━━━━━━━━━━━━\n"
                f"{search_result_text}\n"
                "━━━━━━━━━━━━ 搜索结果结束 ━━━━━━━━━━━━"
            )
            full_notice = mode_notice + "\n" + search_section
        else:
            if needs_search:
                extra = (
                    "【注意：当前联网搜索暂时不可用。请基于你已有知识回答，"
                    "如果信息不确定，直接说明，不要编造。】"
                )
                full_notice = mode_notice + "\n" + extra
            else:
                full_notice = mode_notice

        inserted = False
        for i, m in enumerate(messages):
            if m.get("role") == "system":
                messages[i] = {
                    "role": "system",
                    "content": full_notice + "\n\n" + m["content"],
                }
                inserted = True
                break
        if not inserted:
            messages.insert(0, {"role": "system", "content": full_notice})

        return True

    async def _tool_loop(self, messages: List[Dict[str, Any]], turn: TurnContext, tools: List[Dict[str, Any]]) -> None:
        """Execute tool-call loop until final reply."""
        if tools is None:
            raise RuntimeError("tools cannot be None")

        final_text = ""
        total_tokens = 0
        _failed_skills: Dict[str, int] = {}

        # Gap 6 修复：获取 rate limiter 和 provider
        provider = (turn.model or "").split("/")[0] if turn.model and "/" in (turn.model or "") else "openai"
        rate_limiter = get_rate_limiter()

        # Gap 8 修复：累计成本追踪
        total_cost = 0.0

        # Gap 6：思考计划约束执行
        # 跟踪已调用工具，模型想提前结束时若计划中还有未执行的工具，注入提醒。
        available_tool_names = {
            t.get("function", {}).get("name", "")
            for t in (tools or []) if isinstance(t, dict)
        }
        planned_tools = self._parse_planned_tools(
            turn.meta.get("tool_chain_plan", ""), available_tool_names,
        )
        called_tools: set = set()
        nudged: bool = False  # 只提醒一次，避免无限循环

        # Round 7 修复：熔断器 + 退避 — 在循环外初始化，避免 else 块未定义
        circuit = get_circuit_manager().get(f"llm:{provider}")
        llm_backoff_strategy = llm_backoff()
        dyn_temp = self._compute_dynamic_temperature(turn)

        for i in range(self._max_tool_iterations):
            # Gap 6 修复：LLM API 调用前先获取令牌（rate limit）
            await rate_limiter.acquire(provider)

            # Gap 7 修复：动态温度 — 每轮重新计算（可能因上下文变化调整）
            dyn_temp = self._compute_dynamic_temperature(turn)

            async def _do_llm_call():
                return await self._llm.chat_completion(
                    messages=messages,
                    model=turn.model,
                    max_tokens=turn.token_budget if i == 0 else self._max_tokens,
                    tools=tools or None,
                    temperature=dyn_temp,
                )

            try:
                # 先尝试熔断器 + 退避
                resp = await circuit.acall(
                    lambda: llm_backoff_strategy.retry(_do_llm_call),
                )
            except CircuitOpenError:
                # 熔断器打开 — 触发告警，降级返回
                await get_alert_manager().fire(
                    "circuit_open",
                    title=f"LLM circuit OPEN: {provider}",
                    body=f"模型 {turn.model} 的熔断器已打开，使用降级回复",
                    severity=AlertSeverity.CRITICAL,
                )
                if self._is_zh():
                    final_text = (
                        "⚠️ AI 服务暂时不可用（模型熔断器已触发）\n\n"
                        "可能原因：短时间内请求失败次数过多，服务已自动保护。\n\n"
                        "建议操作：\n"
                        "1. 等待 30-60 秒后重试\n"
                        "2. 输入 /status 查看系统状态\n"
                        "3. 输入 /models 切换其他可用模型\n"
                        "4. 如持续不可用，请联系管理员"
                    )
                else:
                    final_text = (
                        "⚠️ AI service temporarily unavailable (circuit breaker open)\n\n"
                        "Possible cause: Too many failed requests recently, service is in protection mode.\n\n"
                        "Suggestions:\n"
                        "1. Wait 30-60 seconds and retry\n"
                        "2. Type /status to check system status\n"
                        "3. Type /models to switch to another available model\n"
                        "4. Contact administrator if issue persists"
                    )
                turn.result = final_text
                turn.meta["circuit_open"] = True
                return
            except Exception as exc:
                # 退避也失败了 — 触发告警
                await get_alert_manager().fire(
                    "llm_error",
                    title=f"LLM call failed: {provider}",
                    body=str(exc)[:200],
                )
                raise
            tokens_used = int(resp.get("tokens_used") or 0)
            total_tokens += tokens_used

            # Gap 8 修复：按模型定价计算成本
            model_cost_per_1k = MODEL_COST.get(turn.model, MODEL_COST.get("default", 0.002))
            total_cost += (tokens_used / 1000) * model_cost_per_1k

            tool_calls = resp.get("tool_calls") or []

            # Fallback: 当模型不支持 function calling (如 sensenova flash-lite) 时，
            # LLM 可能在纯文本里输出 <web_search query="..."/> 这类自闭合 XML 标签
            # 来"表达"工具调用意图。之前这些标签既不解析也不执行，直接泄漏给用户。
            # 这里补一条解析链路：从输出文本中提取 XML 工具标签 → 转为 tool_calls 结构。
            if not tool_calls and resp.get("text"):
                parsed_calls = self._parse_xml_tool_tags(resp["text"])
                if parsed_calls:
                    # 安全过滤：system_run 需要 OS 模式开启才能执行
                    # 否则即使模型输出了 <system_run/> 标签也不执行
                    if not (self._os_mode_enabled or turn.meta.get("needs_system_access")):
                        parsed_calls = [
                            c for c in parsed_calls if c["name"] != "system_run"
                        ]
                    if parsed_calls:
                        tool_calls = parsed_calls
                        # 记录工具调用来源，供后续 sanitize 使用
                        turn.meta["xml_parsed_tool_calls"] = True
                        logger.info(
                            "tool_loop: parsed %d tool call(s) from XML tags in text output",
                            len(tool_calls),
                        )

            if not tool_calls:
                # Gap 1 修复：流式输出 — 使用 streaming 获取最终回复
                # 尝试流式调用，降级到非流式
                final_text = resp.get("text", "") or ""
                try:
                    if hasattr(self._llm, 'chat_completion_stream'):
                        streamed_parts = []
                        last_emit = 0
                        current_len = 0  # 累积长度计数器, 避免 O(n²) join
                        # 关键 bug 修复：生产端 chat_completion_stream yield 的字段名是
                        # "delta" (见 models/__init__.py 所有 yield 语句), 之前消费端
                        # 写 chunk.get("text", "") 永远取不到值 → 流式静默失效,
                        # 用户看不到打字机效果, _emit_progress 永不触发。
                        # 同时初始化 chunk 防 async for 不执行时 UnboundLocalError。
                        chunk = {}
                        async for chunk in self._llm.chat_completion_stream(
                            messages=messages,
                            model=turn.model,
                            max_tokens=turn.token_budget if i == 0 else self._max_tokens,
                            temperature=dyn_temp,
                        ):
                            delta = chunk.get("delta", "")
                            if delta:
                                streamed_parts.append(delta)
                                current_len += len(delta)
                                # 每 50 个字符发送一次进度事件
                                # 优化: 用计数器避免每次 join, 仅在阈值触发时 join 一次
                                if current_len - last_emit >= 50:
                                    last_emit = current_len
                                    self._emit_progress(
                                        turn, "".join(streamed_parts), "streaming"
                                    )
                            # done 帧携带 tokens_used, 取最终值
                            if chunk.get("done") and chunk.get("tokens_used"):
                                tokens_used = int(chunk["tokens_used"])
                        if streamed_parts:
                            final_text = "".join(streamed_parts)
                except Exception as stream_err:
                    logger.debug("streaming failed, using non-streamed response: %s", stream_err)
                    # fallback to non-streamed response already in final_text

                # Gap 6：模型想结束，但计划中还有工具没调用 → 提醒继续执行
                if (
                    not nudged
                    and planned_tools
                    and i < self._max_tool_iterations - 1
                    and final_text  # 模型确实产出了内容（不是空响应）
                ):
                    missing = [t for t in planned_tools if t not in called_tools]
                    if missing:
                        zh = self._is_zh()
                        if zh:
                            nudge = (
                                f"[系统提示：你之前的工具链规划里还包括这些工具：{', '.join(missing)}，"
                                "但尚未调用。如果这些工具对完成任务有必要，请继续调用；"
                                "如果已不需要，请直接基于现有信息给出最终答复。]"
                            )
                        else:
                            nudge = (
                                f"[System: Your plan still includes these uncalled tools: {', '.join(missing)}. "
                                "Call them if needed; otherwise give the final answer based on current info.]"
                            )
                        messages.append({"role": "assistant", "content": final_text})
                        messages.append({"role": "user", "content": nudge})
                        nudged = True
                        turn.meta["plan_nudge_triggered"] = True
                        continue  # 再来一轮，不 break

                if final_text:
                    messages.append({"role": "assistant", "content": final_text})
                break

            # 记录本轮调用的工具名
            for tc in tool_calls:
                nm = tc.get("name") or tc.get("function", {}).get("name", "")
                if nm:
                    called_tools.add(nm)

            # Gap 5 修复：语义去重。检测与历史调用完全相同的工具+参数组合，
            # 跳过重复调用，直接注入缓存结果提示。
            deduped_calls = []
            for tc in tool_calls:
                nm = tc.get("name") or tc.get("function", {}).get("name", "")
                args_str = tc.get("args") or tc.get("function", {}).get("arguments", "{}")
                if isinstance(args_str, dict):
                    args_str = str(args_str)
                dedup_key = f"{nm}:{args_str[:200]}"
                if dedup_key in called_tools:
                    # 重复调用 → 注入提示而不是执行
                    zh = self._is_zh()
                    if zh:
                        hint = f"[系统提示：工具 {nm} 已用相同参数调用过，请勿重复。]"
                    else:
                        hint = f"[System: Tool {nm} was already called with the same args. Do not repeat.]"
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", nm), "name": nm, "content": hint})
                    turn.meta.setdefault("dedup_skipped", []).append(nm)
                else:
                    deduped_calls.append(tc)
                    called_tools.add(dedup_key)

            if deduped_calls:
                await self._execute_tool_calls(messages, turn, deduped_calls, _failed_skills, i)

            # Gap 3 修复：函数调用自修正 — 对失败的工具调用，让 LLM 修正参数后重试
            if i < self._max_tool_iterations - 1:
                retry_count = turn.meta.get("auto_retry_count", 0)
                if retry_count < MAX_TOOL_RETRIES:
                    failed_in_round = [
                        tc for tc in tool_calls
                        if _failed_skills.get(
                            tc.get("name") or tc.get("function", {}).get("name", ""), 0
                        ) > 0
                    ]
                    if failed_in_round:
                        # 构造自修正提示：把失败的工具调用和错误信息发给 LLM
                        # 从 turn.meta["tool_results"] 中提取最新的失败结果，获取错误原因
                        recent_results = turn.meta.get("tool_results", [])
                        failed_info = ""
                        for tc in failed_in_round[:3]:
                            nm = tc.get("name") or tc.get("function", {}).get("name", "")
                            args = tc.get("args") or tc.get("function", {}).get("arguments", "{}")
                            # 查找该工具最近的错误信息
                            err_msg = ""
                            for r in reversed(recent_results):
                                if getattr(r, "tool_name", "") == nm and getattr(r, "status", "") in ("error", "unavailable"):
                                    err_msg = getattr(r, "error", "") or getattr(r, "data", "")
                                    if err_msg:
                                        err_msg = err_msg[:200]  # 截断过长的错误信息
                                    break
                            if err_msg:
                                failed_info += f"\n- {nm}({args}) → 失败，原因: {err_msg}"
                            else:
                                failed_info += f"\n- {nm}({args}) → 失败"
                        zh = self._is_zh()
                        if zh:
                            retry_prompt = (
                                f"[系统提示：以下工具调用失败了，请修正参数后重试：{failed_info}\n"
                                "如果是参数格式问题，请修正格式；如果是参数值不对，请调整值；"
                                "如果确定该工具无法完成此任务，请换用其他工具或直接回答。]"
                            )
                        else:
                            retry_prompt = (
                                f"[System: The following tool calls failed, fix the arguments and retry: {failed_info}\n"
                                "If it's a format issue, fix the format; if the value is wrong, adjust it; "
                                "if this tool truly can't handle this task, switch tools or answer directly.]"
                            )
                        messages.append({"role": "user", "content": retry_prompt})
                        turn.meta["auto_retry_count"] = retry_count + 1
                        turn.meta["auto_retry_triggered"] = True
                        logger.debug("auto-retry: triggered for %d failed tools (attempt %d)",
                                   len(failed_in_round), retry_count + 1)
                        continue  # 让 LLM 修正后重试同一轮

            # Gap 3：动态重规划 — 检测本轮工具失败，注入重规划提示
            # 之前工具失败只是记录在 messages 里，模型不一定主动调整策略。
            # 现在检测到失败后注入明确的"请换方案"提示，让模型重新规划后续步骤。
            if i < self._max_tool_iterations - 1:
                this_round_names = [tc.get("name") or tc.get("function", {}).get("name", "") for tc in tool_calls]
                any_failed = any(
                    _failed_skills.get(nm, 0) > 0 for nm in this_round_names
                )
                if any_failed:
                    zh = self._is_zh()
                    if zh:
                        replan_msg = (
                            "[系统提示：上一轮有工具调用失败。"
                            "请重新评估当前情况，考虑：换一个工具、换一种参数、或基于已有信息直接给出答案。"
                            "不要重复调用已经失败的工具。]"
                        )
                    else:
                        replan_msg = (
                            "[System: Some tools in the last round failed. "
                            "Re-evaluate and consider: switch to a different tool, change parameters, "
                            "or answer based on available information. Do not retry failed tools.]"
                        )
                    messages.append({"role": "user", "content": replan_msg})
                    turn.meta["replan_triggered"] = True
                    logger.debug("replan triggered after failures: %s", this_round_names)

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
            await rate_limiter.acquire(provider)
            try:
                resp = await circuit.acall(
                    lambda: llm_backoff_strategy.retry(
                        lambda: self._llm.chat_completion(
                            messages=messages, model=turn.model, max_tokens=self._max_tokens,
                            temperature=dyn_temp,
                        )
                    ),
                )
            except CircuitOpenError:
                if self._is_zh():
                    final_text = (
                        "⚠️ AI 服务暂时不可用（模型熔断器已触发）\n\n"
                        "建议：等待 30-60 秒后重试，或输入 /models 切换其他模型。"
                    )
                else:
                    final_text = (
                        "⚠️ AI service temporarily unavailable (circuit breaker open)\n\n"
                        "Suggestion: Wait 30-60 seconds and retry, or type /models to switch."
                    )
                turn.result = final_text
                turn.meta["circuit_open"] = True
                return
            final_text = resp.get("text", "") or (
                "抱歉，AI 未能生成有效回复，请重试或换个方式提问。"
                if self._is_zh() else
                "Sorry, the AI couldn't generate a valid response. Please retry or rephrase."
            )
            tokens_used = int(resp.get("tokens_used") or 0)
            total_tokens += tokens_used
            total_cost += (tokens_used / 1000) * MODEL_COST.get(turn.model, MODEL_COST.get("default", 0.002))

        # 记录成本信息（Gap 8）
        turn.meta["cost"] = {
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "model": turn.model,
            "cost_per_1k": MODEL_COST.get(turn.model, MODEL_COST.get("default", 0.002)),
        }

        # 记录计划完成度（供 self-improvement 分析）
        if planned_tools:
            turn.meta["plan_completion"] = {
                "planned": planned_tools,
                "called": sorted(called_tools & set(planned_tools)),
                "missing": sorted(set(planned_tools) - called_tools),
                "nudged": nudged,
            }

        # Record failure for self-improvement
        if turn.result is None and turn.error:
            # 用异步版本：真正闭环（record_failure + LLM 提炼改进 + apply_improvement）
            await self._record_self_improvement_async(turn)

        if not final_text:
            final_text = (
                "抱歉，AI 未能生成回复，请重试。"
                if self._is_zh() else
                "Sorry, the AI couldn't generate a reply. Please try again."
            )

        # Gap 3 修复：输出安全扫描 — 检测输出中是否泄露 PII
        output_safety = scan_output(final_text)
        if output_safety.pii_found:
            final_text = output_safety.sanitized_text
            logger.warning("output safety: detected %d PII type(s) in output, sanitized",
                          len(output_safety.pii_found))
            turn.meta["output_safety"] = {
                "pii_redacted": len(output_safety.pii_found),
                "types": [p["type"] for p in output_safety.pii_found],
            }

        turn.record_success(final_text, total_tokens)

    def _parse_planned_tools(
        self, plan_text: str, available_tool_names: set,
    ) -> List[str]:
        """Gap 6：从工具链规划文本中提取预期的工具调用顺序。

        匹配规则：规划里出现的、且当前确实可用的工具名，按首次出现顺序返回。
        没有规划或匹配不到时返回空列表（不约束）。
        """
        if not plan_text or not available_tool_names:
            return []
        ordered: List[str] = []
        seen: set = set()
        # 工具名通常是 word-boundary 的标识符（如 web_search、calc、system_run）
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", plan_text):
            if name in available_tool_names and name not in seen:
                ordered.append(name)
                seen.add(name)
        return ordered

    async def _execute_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        turn: TurnContext,
        tool_calls: List[Dict[str, Any]],
        failed_skills: Dict[str, int],
        iteration: int,
    ) -> None:
        """Execute tool calls in parallel and append results to messages.

        Gap 修复：之前是串行 for 循环，每个工具调用等前一个完成。
        现在用 asyncio.gather 并行执行所有独立工具调用。
        如果 3 个独立搜索各花 2 秒，现在总耗时 ~2 秒而不是 ~6 秒。
        """
        if tool_calls is None:
            raise RuntimeError("tool_calls cannot be None")
        if failed_skills is None:
            raise RuntimeError("failed_skills cannot be None")
        if iteration < 0:
            raise RuntimeError("iteration must be non-negative")

        provider = turn.model.split("/")[0] if turn.model and "/" in turn.model else "openai"

        # Gap 5 修复：获取工具结果缓存
        tool_cache = get_tool_cache()

        # 提取所有工具调用信息
        tc_info = []
        for idx, tc in enumerate(tool_calls):
            name = tc.get("name") or ""
            args = tc.get("args") or {}
            tc_id = tc.get("id") or f"call_{idx}"
            tc_info.append((idx, tc, name, args, tc_id))

        # Gap 9 修复：部分结果追踪
        success_count = 0
        failure_count = 0

        if provider == "anthropic":
            # 并行执行所有工具调用（Gap 5: 先查缓存, Round 7: backoff 重试）
            from core.backoff import tool_backoff
            # Round 8: 断路器 — 跳过持续失败的工具
            fr = self._get_failure_recovery()
            async def _run_one(tc, name, args):
                # Round 8: 断路器检查
                if fr.is_circuit_open(f"tool:{name}"):
                    logger.warning("circuit_breaker: skipping tool %s (circuit open)", name)
                    return ToolResult(tool_name=name, status="error",
                                      error=f"工具 {name} 近期多次失败，已自动跳过")
                cached = tool_cache.get(name, args)
                if cached is not None:
                    logger.debug("tool_cache: hit for %s", name)
                    return ToolResult(tool_name=name, status="success", data=cached)
                # Round 7: tool 执行 backoff 重试
                tb = tool_backoff()
                async def _do_dispatch():
                    result = await self._dispatch_smart(tc, name, args, failed_skills)
                    if result.status == "error":
                        raise RuntimeError(f"tool {name} error: {result.error}")
                    return result
                try:
                    result = await tb.retry(_do_dispatch)
                except Exception as exc:
                    result = ToolResult(tool_name=name, status="error", error=str(exc))
                    # Round 8: 记录失败到断路器
                    fr._record_failure(f"tool:{name}", exc)
                if result.status == "success":
                    tool_cache.set(name, args, str(result.data or ""))
                    fr._record_success(f"tool:{name}")
                return result
            coros = [_run_one(tc, name, args) for _, tc, name, args, _ in tc_info]
            results = await asyncio.gather(*coros, return_exceptions=True)

            for idx, (_, tc, name, args, tc_id) in enumerate(tc_info):
                result = results[idx]
                if isinstance(result, Exception):
                    result = ToolResult(
                        tool_name=name,
                        status="error",
                        error=str(result),
                    )
                # Gap 9 修复：追踪成功/失败计数
                if result.status == "success":
                    success_count += 1
                else:
                    failure_count += 1
                if getattr(turn, "estimated_complexity", 0) >= COMPLEX_COMPLEXITY_THRESHOLD:
                    if iteration > 0:
                        self._emit_progress(turn, f"正在调用工具（第{iteration+1}轮）: {name}", "tool_call")
                    else:
                        self._emit_progress(turn, f"正在调用工具: {name}", "tool_call")
                if result.status == "unavailable" and self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver:
                    self.ctx.self_improver.record_failure(
                        user_input=turn.input_text,
                        error_type="tool_unavailable",
                        error_detail=f"Tool {name} unavailable",
                    )
                turn.meta.setdefault("tool_results", []).append(result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result.to_message(),
                })
        else:
            raw_tool_calls = turn.meta.get("tool_calls_raw") or tool_calls
            messages.append({
                "role": "assistant",
                "content": None if provider == "openai" else "",
                "tool_calls": raw_tool_calls,
            })
            # 并行执行所有工具调用（Gap 5: 先查缓存, Round 7: backoff 重试）
            from core.backoff import tool_backoff
            # Round 8: 断路器 — 跳过持续失败的工具
            fr = self._get_failure_recovery()
            async def _run_one(tc, name, args):
                # Round 8: 断路器检查
                if fr.is_circuit_open(f"tool:{name}"):
                    logger.warning("circuit_breaker: skipping tool %s (circuit open)", name)
                    return ToolResult(tool_name=name, status="error",
                                      error=f"工具 {name} 近期多次失败，已自动跳过")
                cached = tool_cache.get(name, args)
                if cached is not None:
                    logger.debug("tool_cache: hit for %s", name)
                    return ToolResult(tool_name=name, status="success", data=cached)
                # Round 7: tool 执行 backoff 重试
                tb = tool_backoff()
                async def _do_dispatch():
                    result = await self._dispatch_smart(tc, name, args, failed_skills)
                    if result.status == "error":
                        raise RuntimeError(f"tool {name} error: {result.error}")
                    return result
                try:
                    result = await tb.retry(_do_dispatch)
                except Exception as exc:
                    result = ToolResult(tool_name=name, status="error", error=str(exc))
                    # Round 8: 记录失败到断路器
                    fr._record_failure(f"tool:{name}", exc)
                if result.status == "success":
                    tool_cache.set(name, args, str(result.data or ""))
                    fr._record_success(f"tool:{name}")
                return result
            coros = [_run_one(tc, name, args) for _, tc, name, args, _ in tc_info]
            results = await asyncio.gather(*coros, return_exceptions=True)

            for idx, (_, tc, name, args, tc_id) in enumerate(tc_info):
                result = results[idx]
                if isinstance(result, Exception):
                    result = ToolResult(
                        tool_name=name,
                        status="error",
                        error=str(result),
                    )
                # Gap 9 修复：追踪成功/失败计数
                if result.status == "success":
                    success_count += 1
                else:
                    failure_count += 1
                if getattr(turn, "estimated_complexity", 0) >= COMPLEX_COMPLEXITY_THRESHOLD:
                    if iteration > 0:
                        self._emit_progress(turn, f"正在调用工具（第{iteration+1}轮）: {name}", "tool_call")
                    else:
                        self._emit_progress(turn, f"正在调用工具: {name}", "tool_call")
                if result.status == "unavailable" and self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver:
                    self.ctx.self_improver.record_failure(
                        user_input=turn.input_text,
                        error_type="tool_unavailable",
                        error_detail=f"Tool {name} unavailable",
                    )
                turn.meta.setdefault("tool_results", []).append(result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": name,
                    "content": result.to_message(),
                })

        # Gap 9 修复：部分结果优雅降级 — 当有工具成功和失败时，注入提示
        total_calls = success_count + failure_count
        if 0 < success_count < total_calls:
            zh = self._is_zh()
            if zh:
                partial_hint = (
                    f"[系统提示：本轮 {total_calls} 个工具调用中，{success_count} 个成功，"
                    f"{failure_count} 个失败。对于成功的工具请基于其结果继续，"
                    f"对于失败的工具请考虑替代方案或基于已有信息回答。]"
                )
            else:
                partial_hint = (
                    f"[System: {success_count}/{total_calls} tool calls succeeded, "
                    f"{failure_count} failed. Use successful results and consider "
                    f"alternatives for failed ones, or answer based on available info.]"
                )
            messages.append({"role": "user", "content": partial_hint})
            turn.meta.setdefault("partial_results", []).append({
                "iteration": iteration,
                "success": success_count,
                "failure": failure_count,
            })
            logger.debug("partial results: %d/%d succeeded in iteration %d",
                       success_count, total_calls, iteration)

    async def _handle_loop_exhaustion(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Handle when tool loop reaches max iterations."""
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

    @staticmethod
    def _parse_error(turn_error) -> tuple:
        """Parse turn.error into (error_type, error_detail) tuple.

        turn.error can be str, dict, or None. Returns normalized strings.
        """
        if isinstance(turn_error, str):
            return turn_error, turn_error
        if isinstance(turn_error, dict):
            return (
                turn_error.get("type", "unknown"),
                turn_error.get("detail", str(turn_error)),
            )
        return "unknown", str(turn_error or "unknown")

    async def _record_self_improvement_async(self, turn: TurnContext) -> None:
        """Record failure + 用 LLM 提炼改进 + 持久化应用（真正闭环）。

        之前 _record_self_improvement 只调 record_failure 写库，
        generate_improvement / apply_improvement 从不被调用 →
        失败→学习→改进行为闭环断裂。现在：
        1. record_failure 写库
        2. 每 5 次失败触发一次 LLM 改进生成
        3. apply_improvement 持久化到 DB
        4. 下一轮 _prepare_messages 通过 get_active_improvements 注入
        """
        assert turn is not None, "turn cannot be None"

        if not (self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver):
            return

        error_type, error_detail = self._parse_error(turn.error)

        self.ctx.self_improver.record_failure(
            user_input=turn.input_text,
            error_type=error_type,
            error_detail=error_detail,
            turn_meta=turn.meta,
        )

        # 每 5 次失败触发一次 LLM 改进生成（避免每次失败都调 LLM 浪费 token）
        try:
            stats = self.ctx.self_improver.get_stats()
            total_failures = stats.get("total_failures", 0)
            if total_failures > 0 and total_failures % 5 == 0 and self._llm is not None:
                suggestion = await self.ctx.self_improver.generate_improvement_async(
                    self._llm,
                )
                if suggestion:
                    self.ctx.self_improver.apply_improvement("llm_analyzed", suggestion)
                    logger.info("self-improvement: 已生成并应用改进建议: %s", suggestion[:80])
        except Exception as exc:
            logger.debug("self-improvement LLM 生成失败: %s", exc)

    def _record_self_improvement(self, turn: TurnContext) -> None:
        """Record failure for self-improvement analysis（同步兼容包装）。

        Fire-and-forget: 启动异步版本，不等待结果，避免阻塞调用方。
        真正的闭环逻辑在 _record_self_improvement_async 里。
        """
        assert turn is not None, "turn cannot be None"
        if not (self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver):
            return
        try:
            asyncio.create_task(self._record_self_improvement_async(turn))
        except RuntimeError:
            # 没有运行中的事件循环时，退化到同步 record_failure
            error_type, error_detail = self._parse_error(turn.error)
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

    def _detect_output_format(self, answer: str) -> str:
        """Gap 修复：检测回复类型，返回格式提示。

        之前 verify_and_polish 的 prompt 只说"优化结构"，LLM 可能把代码块
        优化成纯文本描述、把表格优化成段落。现在先检测格式类型，注入提示保格式。
        """
        if "```" in answer and ("def " in answer or "class " in answer or "import " in answer):
            return "代码块格式（保留 ``` 代码块）"
        if "```" in answer:
            return "代码块格式"
        if "|" in answer and "---" in answer:
            return "表格格式"
        if re.search(r"^\d+\.\s", answer, re.MULTILINE) or re.search(r"^-\s", answer, re.MULTILINE):
            if len(answer) > 500:
                return "列表+要点总结格式"
            return "列表格式"
        if "http" in answer and len(answer) > 300:
            return "保留链接和引用"
        return ""

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

        # Gap 4 修复：输出格式智能。检测回复类型，在优化 prompt 中注入格式要求。
        fmt_hint = self._detect_output_format(answer)
        if fmt_hint and zh:
            fmt_hint = f"请保持以下格式：{fmt_hint}。\n"
        elif fmt_hint:
            fmt_hint = f"Preserve this format: {fmt_hint}.\n"
        else:
            fmt_hint = ""

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
                    f"{fmt_hint}"
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
                    f"{fmt_hint}"
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

    async def _objective_verify(self, turn: TurnContext) -> None:
        """Gap 修复：输出客观验证（不只是自问自答）。

        之前的 _verify_and_polish 是让同一个模型审查自己的输出，
        对"模型不知道自己错了"的情况完全无效。现在添加客观验证：
        1. 如果答案引用了搜索结果，检查是否与搜索结果一致
        2. 如果答案包含代码，尝试用 python_execute 验证
        3. 检测数字/事实是否有明显矛盾
        """
        if not turn.result or not self._skills:
            return

        answer = turn.result or ""
        tool_results = turn.meta.get("tool_results", [])
        zh = self._is_zh()

        # 1. 交叉验证搜索来源：如果用了 web_search，检查答案是否与搜索结果一致
        search_snippets = []
        for tr in tool_results:
            tr_data = str(tr.data) if hasattr(tr, 'data') and tr.data else ""
            if tr_data and len(tr_data) > 50:
                search_snippets.append(tr_data[:500])

        if search_snippets and self._llm:
            snippet_text = "\n".join(search_snippets[:3])
            try:
                verify_prompt = (
                    "你是事实核查员。请对比以下搜索结果和 AI 回答，"
                    "检查 AI 回答中是否有与搜索结果矛盾的地方。"
                    "如果一致，回复 PASS。如果有矛盾，用 1 句话指出。"
                    if zh else
                    "You are a fact-checker. Compare the search results below "
                    "with the AI answer. If consistent, reply PASS. "
                    "If there's a contradiction, point it out in 1 sentence."
                )
                resp = await self._llm.chat_completion(
                    messages=[
                        {"role": "system", "content": verify_prompt},
                        {"role": "user", "content": (
                            f"搜索结果：\n{snippet_text[:2000]}\n\n"
                            f"AI 回答：\n{answer[:1500]}"
                        )},
                    ],
                    model=turn.model,
                    max_tokens=150,
                    tools=None,
                )
                fb = (resp.get("text") or "").strip()
                if fb and "PASS" not in fb.upper() and "pass" not in fb.lower():
                    turn.meta["fact_check"] = "flagged"
                    turn.meta["fact_check_detail"] = fb[:200]
                    logger.info("objective verification flagged: %.100s", fb)
                else:
                    turn.meta["fact_check"] = "passed"
            except Exception as exc:
                logger.debug("objective verification LLM call failed: %s", exc)

        # 2. 代码检测：答案中有代码块时标记
        if "```" in answer and ("def " in answer or "import " in answer or "class " in answer):
            turn.meta["contains_code"] = True
            # 如果答案中有代码且有 python_execute 工具可用，尝试验证
            if self._skills.get("python_execute"):
                # 提取代码块
                import re as _re
                code_blocks = _re.findall(r"```(?:python)?\n(.*?)```", answer, _re.DOTALL)
                if code_blocks:
                    turn.meta["code_blocks_found"] = len(code_blocks)
                    turn.meta["code_verifiable"] = True

        # 3. 数字/事实合理性快速检查
        # 检测明显矛盾（如 "100% 的同时又说 80%"）
        percentages = []
        import re as _re2
        for m in _re2.finditer(r"(\d+)%", answer):
            val = int(m.group(1))
            if val > 100:
                turn.meta.setdefault("suspicious_patterns", []).append(f"百分比超过100%: {val}%")
            percentages.append(val)
        if len(percentages) >= 2 and max(percentages) > 100 and sum(percentages) - max(percentages) > 80:
            turn.meta.setdefault("suspicious_patterns", []).append("多个百分比可能不兼容")

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

    # ======================================================== Intelligent Features

    def _get_sentiment(self) -> Any:
        """Lazy-load sentiment analyzer."""
        if self._sentiment is None:
            self._sentiment = get_sentiment_analyzer()
        return self._sentiment

    def _get_suggestions(self) -> Any:
        """Lazy-load suggestion engine."""
        if self._suggestions is None:
            self._suggestions = get_suggestion_engine()
        return self._suggestions

    def _get_profile(self) -> Any:
        """Lazy-load user profile store."""
        if self._profile is None:
            self._profile = get_profile_store()
        return self._profile

    def _get_metacognition(self) -> Any:
        """Lazy-load metacognition engine."""
        if self._metacognition is None:
            self._metacognition = get_metacognition_engine()
        return self._metacognition

    def _get_reasoner(self) -> Any:
        """Lazy-load step-by-step reasoner."""
        if self._reasoner is None:
            self._reasoner = get_step_reasoner()
        return self._reasoner

    def _get_style_adapter(self) -> StyleAdapter:
        """Lazy-load style adapter."""
        if self._style_adapter is None:
            self._style_adapter = StyleAdapter()
            # Try to load style from user profile
            try:
                profile = self._get_profile()
                saved_style = profile.get_preference("response_style")
                if saved_style:
                    self._style_adapter.set_style(saved_style)
            except Exception:
                pass
        return self._style_adapter

    def _get_failure_recovery(self) -> Any:
        """Lazy-load failure recovery manager."""
        if self._failure_recovery is None:
            self._failure_recovery = get_failure_recovery()
        return self._failure_recovery

    def _get_dialog_summarizer(self) -> Any:
        """Lazy-load dialog summarizer."""
        if self._dialog_summarizer is None:
            self._dialog_summarizer = get_dialog_summarizer()
        return self._dialog_summarizer

    async def _record_intelligence(self, turn: TurnContext) -> None:
        """Record user preferences, sentiment, and patterns."""
        if not turn.input_text or not turn.result:
            return

        try:
            # 1. Analyze sentiment
            sentiment = self._get_sentiment()
            analysis = await asyncio.to_thread(sentiment.analyze, turn.input_text)
            turn.meta["sentiment"] = analysis

            # 2. Record skill usage (from tool_calls in meta)
            profile = self._get_profile()
            skills_used = turn.meta.get("skills_used", [])
            for skill in skills_used:
                success = "error" not in str(turn.result).lower()
                await asyncio.to_thread(profile.record_skill_usage, skill, success)

            # 3. Record time pattern
            await asyncio.to_thread(profile.record_time_pattern)

            # 4. Extract and record topics (simple keyword extraction)
            topics = self._extract_topics(turn.input_text)
            for topic in topics[:3]:
                await asyncio.to_thread(profile.record_topic, topic)

            # 5. Update language preference if detected
            if hasattr(turn, "detected_lang"):
                await asyncio.to_thread(
                    profile.set_preference, "language", turn.detected_lang
                )

            # 6. Track dialog summary turn counter
            summarizer = self._get_dialog_summarizer()
            turn_count = summarizer.increment_turn(turn.session_id)
            turn.meta["turn_count"] = turn_count

            # 7. 对话摘要 — 每 N 轮生成一次摘要，下轮注入上下文
            if summarizer.should_summarize(turn.session_id):
                try:
                    # 从 turn.messages 中提取 user/assistant 对话对
                    history_for_summary = []
                    for msg in turn.messages:
                        role = msg.get("role", "")
                        content = msg.get("content", "")
                        if role == "user" and content and not content.startswith("["):
                            history_for_summary.append({"input": content, "reply": ""})
                        elif role == "assistant" and content and history_for_summary:
                            history_for_summary[-1]["reply"] = content

                    if history_for_summary:
                        existing = (summarizer.get_summary(turn.session_id) or {}).get("summary", "")
                        lang = "zh" if self._is_zh() else "en"
                        prompt = summarizer.generate_summary_prompt(
                            history_for_summary[-10:], existing, lang
                        )
                        summary_resp = await self._llm.chat_completion(
                            messages=[{"role": "user", "content": prompt}],
                            model=self._lightweight_model(turn),
                            temperature=0.2,
                            max_tokens=400,
                            use_cache=False,
                        )
                        summary_text = summary_resp.get("text", "").strip()
                        if summary_text:
                            summarizer.store_summary(turn.session_id, summary_text, turn_count)
                            logger.debug("dialog summary generated for session %s (%d turns)",
                                        turn.session_id, turn_count)
                except Exception as exc:
                    logger.debug("dialog summary generation failed: %s", exc)

            # 8. 风格自适应学习：检测用户对回复风格的自然语言反馈
            # （"太啰嗦了"/"详细一点"/"别用 emoji"/"专业点"等），自动调整
            # StyleAdapter 并持久化到用户画像，下一轮 _prepare_messages 即生效。
            # 这让 agent 能听懂用户的风格偏好，无需任何手动配置。
            try:
                style_adapter = self._get_style_adapter()
                updates = style_adapter.adjust_from_feedback(turn.input_text)
                if updates:
                    profile = self._get_profile()
                    current = profile.get_preference("response_style") or {}
                    if isinstance(current, dict):
                        current.update(updates)
                    else:
                        current = dict(updates)
                    await asyncio.to_thread(
                        profile.set_preference, "response_style", current
                    )
                    turn.meta["style_adjusted"] = updates
                    logger.debug("style auto-adjusted from feedback: %s", updates)
            except Exception as exc:
                logger.debug("style auto-learning skipped: %s", exc)

        except Exception as exc:
            logger.debug("intelligence recording failed: %s", exc)

    def _extract_topics(self, text: str) -> List[str]:
        """Extract topic keywords from user input."""
        # Simple keyword-based topic extraction
        # In production, could use NLP/LLM for better extraction
        topics = []

        # Technical topics
        tech_patterns = [
            (r"代码|编程|python|javascript|java|rust", "编程"),
            (r"搜索|查找|查询|search", "搜索"),
            (r"文档|文件|file|document", "文档"),
            (r"系统|shell|命令|command", "系统"),
            (r"计算|数学|math|calc", "计算"),
            (r"图片|图像|image|photo", "图片"),
            (r"音频|语音|audio|voice", "音频"),
            (r"笔记|记录|note|save", "笔记"),
        ]

        for pattern, topic in tech_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                topics.append(topic)

        return topics

    async def _generate_suggestions(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Generate proactive suggestions based on context."""
        try:
            suggestions_engine = self._get_suggestions()
            skills_used = turn.meta.get("skills_used", [])
            suggestions = await asyncio.to_thread(
                suggestions_engine.generate_suggestions,
                turn.input_text,
                turn.result,
                skills_used,
                {"complexity": getattr(turn, "estimated_complexity", 0.0)},
            )
            return suggestions
        except Exception as exc:
            logger.debug("suggestion generation failed: %s", exc)
            return []

    def _should_show_suggestions(self) -> bool:
        """Check if suggestions should be displayed to user."""
        # Check config for suggestion display preference
        if self.ctx and self.ctx.config:
            return self.ctx.config.get("agent", {}).get("show_suggestions", True)
        return True  # Default: show suggestions

    def _inject_sentiment_context(
        self, messages: List[Dict[str, Any]], turn: TurnContext
    ) -> None:
        """Inject sentiment analysis into LLM context."""
        sentiment_data = turn.meta.get("sentiment")
        if not sentiment_data:
            return

        sentiment = self._get_sentiment()
        sentiment_text = sentiment.format_for_llm(sentiment_data)
        if sentiment_text:
            # Inject as a system hint before user message
            for i, msg in enumerate(messages):
                if msg.get("role") == "user" and i > 0:
                    messages.insert(i, {"role": "system", "content": sentiment_text})
                    break

    # --------------------------------------------------------- Metacognition

    async def _analyze_response_quality(self, turn: TurnContext) -> None:
        """Analyze response quality using metacognition engine."""
        if not turn.result:
            return

        try:
            metacog = self._get_metacognition()
            skills_used = turn.meta.get("skills_used", [])

            # 优先用 LLM 做真正的置信度评估（之前 evaluate_with_llm 定义了
            # 但从未被调用，一直用纯正则匹配统计关键词——容易被 LLM 的
            # 流利编造绕过）。LLM 可用时用它，不可用时回退到正则。
            l_analysis = await metacog.evaluate_with_llm(
                self._llm,
                turn.result,
                turn.input_text,
                [],  # sources_used
            )
            # evaluate_with_llm 返回 {confidence, reason, flags, source}
            # 不同于 analyze_response 返回 {confidence, hallucination_risk, ...}
            # 需要统一字段
            confidence = l_analysis.get("confidence", 0.5)
            flags = l_analysis.get("flags", [])
            if isinstance(flags, str):
                flags = [flags]
            if confidence < 0.3:
                hallucination_risk = "high"
            elif confidence < 0.5:
                hallucination_risk = "medium"
            else:
                hallucination_risk = "low"
            analysis = {
                "confidence": confidence,
                "hallucination_risk": hallucination_risk,
                "flags": flags,
                "reason": l_analysis.get("reason", ""),
                "source": l_analysis.get("source", "llm"),
                "caution_topics": [],
            }
            turn.meta["metacognition"] = analysis

            # 置信度极低或高幻觉风险 → 自动重试一次，换不同的 prompt 策略
            # 之前只存 metadata 不行动，低质量回答直接发给用户。
            # 现在闭环：检测到问题 → 自动重答 → 给用户更好的答案。
            _should_retry = (
                confidence < 0.35
                or hallucination_risk == "high"
                or (confidence < 0.5 and hallucination_risk == "medium")
            )
            if _should_retry and self._llm is not None:
                logger.info(
                    "metacognition: low confidence (%.2f), auto-retrying...",
                    confidence,
                )
                try:
                    # 用原始上下文（system + 历史 + 当前输入）重试，而非单条消息
                    # 之前 retry_messages 只有一条 user 消息，丢失全部上下文导致重答质量更差
                    zh = self._is_zh()
                    retry_hint = (
                        "请重新回答上述问题，确保准确、有据可查。如果不确定，请明确说明。"
                        if zh else
                        "Please re-answer the above question, ensuring accuracy and citing sources. If unsure, say so."
                    )
                    retry_messages = list(turn.messages) + [
                        {"role": "assistant", "content": turn.result or ""},
                        {"role": "user", "content": retry_hint},
                    ]
                    retry_result = await self._llm.chat_completion(
                        messages=retry_messages,
                        model=turn.model,
                        temperature=0.1,  # 更低的温度提高准确性
                        max_tokens=getattr(self, "_max_tokens", 2048) or 2048,
                        tools=None,
                        use_cache=False,
                    )
                    retry_text = (retry_result.get("text") or "").strip()
                    if retry_text and len(retry_text) > 20:
                        turn.result = (
                            f"[自动重答 — 原回答置信度 {confidence:.0%}]\n\n{retry_text}"
                        )
                        turn.meta["auto_retry_from_metacognition"] = True
                        turn.meta["original_confidence"] = confidence
                        logger.info("metacognition: auto-retry succeeded")
                except Exception as retry_exc:
                    logger.debug("metacognition auto-retry failed: %s", retry_exc)

            # 置信度仍然不高时，加 disclaimer 提醒用户
            conf_note = metacog.format_confidence_note(analysis)
            if conf_note:
                turn.result = (turn.result or "") + conf_note

            logger.debug(
                "metacognition: confidence=%.2f risk=%s retried=%s",
                confidence, hallucination_risk,
                turn.meta.get("auto_retry_from_metacognition", False),
            )
        except Exception as exc:
            logger.debug("metacognition analysis failed: %s", exc)

    # --------------------------------------------------------- Step-by-step Reasoning

    async def _maybe_inject_cot(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Inject Chain-of-Thought reasoning prompt for complex tasks."""
        try:
            reasoner = self._get_reasoner()
            task_types = reasoner.detect_task_type(turn.input_text)
            complexity = getattr(turn, "estimated_complexity", 0.0)

            if not reasoner.should_use_cot(complexity, task_types):
                return

            # Get available tool names
            tool_names: List[str] = []
            if self._skills is not None:
                tool_names = list(self._skills.list_skills().keys())[:15]  # Limit to avoid too long prompt

            cot_prompt = reasoner.generate_reasoning_prompt(
                turn.input_text,
                task_types,
                tool_names,
            )

            # Inject CoT prompt before the last user message
            # 修复：兼容多模态 list content (vision), 之前 str+list 抛 TypeError
            if messages and messages[-1].get("role") == "user":
                messages[-1]["content"] = self._prepend_to_content(
                    messages[-1]["content"], cot_prompt
                )

            turn.meta["task_types"] = task_types
            turn.meta["cot_enabled"] = True
            logger.debug("CoT reasoning enabled for task types: %s", task_types)
        except Exception as exc:
            logger.debug("CoT injection failed: %s", exc)

    # ================================================================
    # Round 6: New intelligent features
    # ================================================================

    # --------------------------------------------------- Gap 7: Dynamic temperature
    def _compute_dynamic_temperature(self, turn: TurnContext) -> float:
        """Adjust temperature based on task complexity and type.

        - Factual/analytical tasks → lower temperature (more deterministic)
        - Creative/generative tasks → higher temperature (more variety)
        - Default: base temperature
        """
        complexity = getattr(turn, "estimated_complexity", 0.5)

        # 深度审计 P2-7 修复：之前只读 turn.meta["task_types"], 而该字段仅
        # 在 _maybe_inject_cot() 内被赋值, 且 CoT 在低/中复杂度时被跳过 →
        # 大量轮次 task_types 为空, 温度退化为仅按 complexity 打分。
        # 修复：直接调用 reasoner.detect_task_type() 自行检测, 同时写回 meta
        # 供下游 (CoT/规划) 复用, 避免重复检测。
        task_types = turn.meta.get("task_types") or []
        if not task_types:
            try:
                reasoner = self._get_reasoner()
                task_types = reasoner.detect_task_type(turn.input_text)
                if task_types:
                    turn.meta["task_types"] = task_types
            except Exception as exc:
                logger.debug("dynamic_temp: detect_task_type failed: %s", exc)
                task_types = []

        # Creative tasks benefit from higher temperature
        creative_types = {"creative", "writing", "brainstorming", "storytelling", "poetry"}
        if any(t in creative_types for t in task_types):
            return DYNAMIC_TEMP_CREATIVE

        # Factual/analytical tasks need lower temperature
        factual_types = {"factual", "analysis", "coding", "debugging", "math", "calculation"}
        if any(t in factual_types for t in task_types):
            return DYNAMIC_TEMP_FACTUAL

        # Graduated: more complex = slightly lower temperature (precision)
        if complexity >= EXPERT_COMPLEXITY_THRESHOLD:
            return 0.15
        elif complexity >= COMPLEX_COMPLEXITY_THRESHOLD:
            return 0.3
        elif complexity >= SIMPLE_COMPLEXITY_THRESHOLD:
            return 0.5

        return DYNAMIC_TEMP_BASE

    # --------------------------------------------------- Gap 2: Response regeneration
    async def _handle_rewrite(self, turn: TurnContext, args_text: str) -> None:
        """Handle /retry or /重新生成 — regenerate the last response.

        Takes the last turn's context and re-runs with a different temperature
        or approach to produce a fresh answer.
        """
        zh = self._is_zh()
        session_id = turn.session_id

        # Check regeneration limit
        count = self._regeneration_count.get(session_id, 0)
        if count >= REGENERATION_MAX:
            turn.result = (
                f"已达到最大重新生成次数（{REGENERATION_MAX}次）。请提出新的问题。"
                if zh else f"Max regeneration limit reached ({REGENERATION_MAX}). Please ask a new question."
            )
            return

        # Get history from turn meta
        history = turn.meta.get("history", [])
        if not history:
            turn.result = "没有历史记录可供重新生成。" if zh else "No history available for regeneration."
            return

        # Get the last user input
        last_turn = history[-1] if history else {}
        last_input = str(last_turn.get("input", ""))
        if not last_input:
            turn.result = "无法找到上一轮用户输入。" if zh else "Cannot find previous user input."
            return

        self._regeneration_count[session_id] = count + 1

        if self._llm is None:
            turn.result = "[LLM 未初始化]" if zh else "[LLM not initialized]"
            return

        # Use a different temperature for variety
        alt_temp = 0.9 if count % 2 == 0 else 0.3

        # Build minimal context from history
        messages = []
        sys_prompt = turn.meta.get("system_prompt", "")
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        else:
            messages.append({"role": "system", "content": "请用不同的角度和方式重新回答以下问题，生成一个全新的回答。" if zh else "Please re-answer the following question from a different angle and style."})

        messages.append({"role": "user", "content": last_input})

        self._emit_progress(turn, f"正在重新生成（第{count+1}次），温度={alt_temp}...", "rewrite")

        try:
            resp = await self._llm.chat_completion(
                messages=messages,
                model=turn.model,
                temperature=alt_temp,
                max_tokens=DEFAULT_MAX_TOKENS,
                tools=None,
            )
            result = (resp.get("text") or "").strip()
            tokens = int(resp.get("tokens_used") or 0)
            turn.result = result
            turn.record_success(result, tokens)
            turn.meta["regenerated"] = True
            turn.meta["regeneration_count"] = count + 1
            turn.meta["regeneration_temp"] = alt_temp
            logger.info("rewrite_turn: regenerated response (attempt %d, temp=%.1f)", count + 1, alt_temp)
        except Exception as exc:
            turn.record_failure(f"regeneration failed: {exc}")
            turn.result = f"重新生成失败: {exc}" if zh else f"Regeneration failed: {exc}"

    # --------------------------------------------------- Round 8: Auto deep research

    # 研究型任务关键词 — 触发自动深度研究
    _RESEARCH_KEYWORDS = frozenset({
        # 中文
        "研究", "调研", "调查", "分析", "综述", "对比分析", "深入了解",
        "详细分析", "全面分析", "深度分析", "深入研究", "系统性分析",
        "最新进展", "发展现状", "趋势分析", "行业分析", "技术调研",
        # English
        "research", "investigate", "analyze", "analysis", "review",
        "comprehensive", "in-depth", "deep dive", "survey", "study",
        "state of", "latest advances", "trends in", "overview of",
    })

    def _is_research_task(self, text: str) -> bool:
        """检测用户输入是否为研究型任务。

        两个条件（满足其一即可）：
        1. 包含研究关键词
        2. 输入较长（>100字符）且包含问号（暗示复杂问题）
        """
        if not text or len(text) < 10:
            return False
        text_lower = text.lower()
        # 条件 1：关键词匹配
        for kw in self._RESEARCH_KEYWORDS:
            if kw in text_lower:
                return True
        # 条件 2：长问题 + 问号
        if len(text) > 100 and ("?" in text or "？" in text):
            return True
        return False

    async def _auto_deep_research(self, turn: TurnContext) -> bool:
        """自动触发深度研究。

        Returns True if deep research handled the turn (published turn_completed),
        False if it failed and should fall back to normal flow.
        """
        try:
            researcher = get_deep_researcher(self._llm, self._skills)

            def on_progress(phase: str, msg: str) -> None:
                self._emit_progress(turn, msg, f"deep_research/{phase}")

            report = await researcher.research(
                question=turn.input_text,
                model=turn.model,
                depth=2,
                on_progress=on_progress,
            )

            turn.result = researcher.format_report(report)
            turn.meta["deep_research"] = {
                "sub_questions": len(report.sub_questions),
                "sources": len(report.sources),
                "searches": report.total_searches,
                "duration": round(report.duration_seconds, 1),
                "auto_triggered": True,
            }
            turn.record_success(turn.result, 0)
            logger.info("auto deep_research: completed in %.1fs, %d sources",
                       report.duration_seconds, len(report.sources))

            # KG extraction
            if self.ctx and hasattr(self.ctx, 'memory') and hasattr(self.ctx.memory, '_kg') and self.ctx.memory._kg:
                full_text = f"{turn.input_text}\n{turn.result}"
                try:
                    count = self.ctx.memory._kg.extract_from_text(full_text, source=turn.session_id)
                    if count > 0:
                        logger.debug("Extracted %d entities from deep_research turn %s", count, turn.session_id)
                except Exception as exc:
                    logger.debug("KG extraction failed in deep_research: %s", exc)

            self.publish("turn_completed", turn=turn)
            return True

        except Exception as exc:
            logger.warning("auto deep_research failed, falling back to normal flow: %s", exc)
            return False

    # --------------------------------------------------- Gap 6: Deep research handler
    async def _handle_deep_research(self, turn: TurnContext, args_text: str) -> None:
        """Handle /deep or /深度研究 — deep research mode."""
        zh = self._is_zh()
        question = args_text.strip() if args_text else turn.input_text

        if not question or len(question) < 10:
            turn.result = (
                "请提供要研究的问题。用法: /deep <问题>\n示例: /deep 量子计算的最新进展"
                if zh else "Please provide a research question. Usage: /deep <question>\nExample: /deep Latest advances in quantum computing"
            )
            return

        if self._llm is None:
            turn.result = "[LLM 未初始化]" if zh else "[LLM not initialized]"
            return

        try:
            researcher = get_deep_researcher(self._llm, self._skills)

            def on_progress(phase: str, msg: str) -> None:
                self._emit_progress(turn, msg, f"deep_research/{phase}")

            report = await researcher.research(
                question=question,
                model=turn.model,
                depth=2,
                on_progress=on_progress,
            )

            turn.result = researcher.format_report(report)
            turn.meta["deep_research"] = {
                "sub_questions": len(report.sub_questions),
                "sources": len(report.sources),
                "searches": report.total_searches,
                "duration": round(report.duration_seconds, 1),
            }
            turn.record_success(turn.result, 0)
            logger.info("deep_research: completed in %.1fs, %d sources",
                       report.duration_seconds, len(report.sources))
        except Exception as exc:
            turn.record_failure(f"deep research failed: {exc}")
            turn.result = f"深度研究失败: {exc}" if zh else f"Deep research failed: {exc}"

    # --------------------------------------------------- Gap 4: Batch processing handler
    async def _handle_batch(self, turn: TurnContext, args_text: str) -> None:
        """Handle /batch or /批量 — batch processing."""
        zh = self._is_zh()
        text = args_text.strip() if args_text else turn.input_text

        if not text or len(text) < 10:
            turn.result = (
                "请提供要批量处理的内容。用法: /batch <任务类型> <内容>\n"
                "任务类型: translate, summarize, classify, extract\n"
                "示例: /batch translate\n1. Hello World\n2. Good morning\n3. How are you?"
                if zh else "Usage: /batch <task_type> <content>\n"
                "Task types: translate, summarize, classify, extract\n"
                "Example: /batch translate\n1. Hello World\n2. Good morning"
            )
            return

        if self._llm is None:
            turn.result = "[LLM 未初始化]" if zh else "[LLM not initialized]"
            return

        # Parse task type and content
        lines = text.split("\n", 1)
        task_type = "general"
        content = text

        if len(lines) >= 2:
            first_word = lines[0].strip().lower()
            if first_word in ("translate", "翻译", "summarize", "总结", "摘要",
                            "classify", "分类", "extract", "提取", "general"):
                task_type_map = {
                    "translate": "translate", "翻译": "translate",
                    "summarize": "summarize", "总结": "summarize", "摘要": "summarize",
                    "classify": "classify", "分类": "classify",
                    "extract": "extract", "提取": "extract",
                }
                task_type = task_type_map.get(first_word, "general")
                content = lines[1].strip() if len(lines) > 1 else ""

        try:
            processor = get_batch_processor(self._llm, self._skills)
            items = processor.split_items(content)

            if len(items) < 2:
                turn.result = (
                    "批量处理需要至少 2 个项目。请用换行或数字列表分隔。"
                    if zh else "Batch processing requires at least 2 items. Separate by newlines or numbers."
                )
                return

            def on_progress(done: int, total: int, item: str) -> None:
                self._emit_progress(turn, f"批量处理中 ({done}/{total})...", "batch")

            self._emit_progress(turn, f"开始批量处理 {len(items)} 个项目...", "batch")
            result = await processor.process(
                items=items,
                model=turn.model,
                task_type=task_type,
                on_progress=on_progress,
            )

            turn.result = processor.format_result(result)
            turn.meta["batch"] = {
                "total": result.total,
                "succeeded": result.succeeded,
                "failed": result.failed,
                "task_type": task_type,
            }
            turn.record_success(turn.result, 0)
            logger.info("batch: %d/%d succeeded", result.succeeded, result.total)
        except Exception as exc:
            turn.record_failure(f"batch processing failed: {exc}")
            turn.result = f"批量处理失败: {exc}" if zh else f"Batch processing failed: {exc}"

    # --------------------------------------------------- Gap 8: Model comparison handler
    async def _handle_model_compare(self, turn: TurnContext, args_text: str) -> None:
        """Handle /compare or /模型对比 — A/B model comparison."""
        zh = self._is_zh()
        text = args_text.strip() if args_text else turn.input_text

        if not text:
            turn.result = (
                "用法: /compare <模型A> <模型B> <问题>\n"
                "示例: /compare openai/gpt-4o anthropic/claude-3.5-sonnet 量子计算是什么？"
                if zh else "Usage: /compare <modelA> <modelB> <question>\n"
                "Example: /compare openai/gpt-4o anthropic/claude-3.5-sonnet What is quantum computing?"
            )
            return

        if self._llm is None:
            turn.result = "[LLM 未初始化]" if zh else "[LLM not initialized]"
            return

        # Parse: model_a model_b question
        parts = text.split(None, 2)
        if len(parts) < 3:
            # Try to use last question from history
            history = turn.meta.get("history", [])
            if history and len(parts) >= 2:
                last_input = str(history[-1].get("input", ""))
                if last_input:
                    parts = [parts[0], parts[1], last_input]
                else:
                    turn.result = (
                        "请提供: <模型A> <模型B> <问题>"
                        if zh else "Please provide: <modelA> <modelB> <question>"
                    )
                    return
            else:
                turn.result = (
                    "请提供: <模型A> <模型B> <问题>"
                    if zh else "Please provide: <modelA> <modelB> <question>"
                )
                return

        model_a, model_b, question = parts[0], parts[1], parts[2]

        self._emit_progress(turn, f"正在对比 {model_a} vs {model_b}...", "model_compare")

        try:
            comparer = get_model_comparer(self._llm, self._skills)
            result = await comparer.compare(
                question=question,
                model_a=model_a,
                model_b=model_b,
            )

            turn.result = comparer.format_comparison(result)
            turn.meta["model_compare"] = result.to_dict()
            turn.record_success(turn.result, 0)
            logger.info("model_compare: %s vs %s → %s", model_a, model_b, result.winner)
        except Exception as exc:
            turn.record_failure(f"model comparison failed: {exc}")
            turn.result = f"模型对比失败: {exc}" if zh else f"Model comparison failed: {exc}"

    # --------------------------------------------------- Gap 1: Eval handler
    async def _handle_eval(self, turn: TurnContext, args_text: str) -> None:
        """Handle /eval or /评估 — evaluate answer quality."""
        zh = self._is_zh()

        if self._llm is None:
            turn.result = "[LLM 未初始化]" if zh else "[LLM not initialized]"
            return

        harness = get_eval_harness()

        # If args provided, evaluate that. Otherwise, evaluate last answer.
        if args_text.strip():
            question = "评估目标"
            answer = args_text.strip()
        else:
            history = turn.meta.get("history", [])
            if history:
                last = history[-1]
                question = str(last.get("input", "评估目标"))
                answer = str(last.get("reply", ""))
            else:
                turn.result = (
                    "请提供要评估的文本，或先进行一次对话后再评估。用法: /eval <文本>"
                    if zh else "Please provide text to evaluate, or chat first. Usage: /eval <text>"
                )
                return

        self._emit_progress(turn, "正在评估回答质量...", "eval")

        try:
            eval_result = await harness.evaluate(
                llm=self._llm,
                question=question,
                answer=answer,
                judge_model=turn.model,
            )

            dim_lines = "\n".join(f"  {d}: {s:.1f}/10" for d, s in eval_result.scores.items())
            turn.result = (
                f"评估结果\n"
                f"────────────────\n"
                f"总分: {eval_result.overall:.1f}/10\n\n"
                f"各维度评分:\n{dim_lines}\n\n"
                f"评价: {eval_result.judge_reasoning}"
            )
            turn.meta["eval"] = eval_result.to_dict()
            turn.record_success(turn.result, 0)
            logger.info("eval: overall=%.1f", eval_result.overall)
        except Exception as exc:
            turn.record_failure(f"eval failed: {exc}")
            turn.result = f"评估失败: {exc}" if zh else f"Evaluation failed: {exc}"

    # --------------------------------------------------- Gap 5: Multi-turn proactive planning
    async def _proactive_plan(self, turn: TurnContext) -> None:
        """After a turn completes, generate proactive next-step suggestions.

        These are injected into the next turn's context so the LLM can
        anticipate what the user might ask next and prepare accordingly.
        """
        if not self._llm or not turn.result:
            return

        zh = self._is_zh()
        session_id = turn.session_id

        # Only generate plan for complex+ tasks to avoid overhead
        complexity = getattr(turn, "estimated_complexity", 0.0)
        if complexity < COMPLEX_COMPLEXITY_THRESHOLD:
            return

        try:
            prompt = (
                f"用户刚才问了：{turn.input_text[:300]}\n"
                f"你回答的核心内容：{turn.result[:500]}\n\n"
                + (
                    "基于以上对话，预测用户接下来最可能追问的 1-3 个问题或需要的下一步操作。"
                    "每个一行，以 '- ' 开头。只输出预测，不要解释。"
                    if zh else
                    "Based on the above conversation, predict 1-3 follow-up questions or next steps "
                    "the user is most likely to ask. One per line, starting with '- '. Only predictions."
                )
            )

            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=self._lightweight_model(turn),
                temperature=0.3,
                max_tokens=200,
                tools=None,
            )
            plan_text = (resp.get("text") or "").strip()

            if plan_text:
                # Parse lines starting with '- '
                import re
                items = re.findall(r'[-*]\s*(.+)', plan_text)
                if items:
                    self._proactive_plans[session_id] = items[:3]
                    turn.meta["proactive_plan"] = items[:3]
                    logger.debug("proactive_plan: generated %d next-step predictions", len(items))
        except Exception as exc:
            logger.debug("proactive_plan failed: %s", exc)

    def _get_proactive_plan(self, session_id: str) -> Optional[List[str]]:
        """Get the proactive plan for the next turn, then clear it."""
        plan = self._proactive_plans.pop(session_id, None)
        return plan

    # ================================================================
    # Round 7: Production Reliability + Tool Ecosystem + Intelligence Depth
    # ================================================================

    # --------------------------------------------------- Email handler
    async def _handle_email(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在处理邮件...", "email")
        zh = self._is_zh()
        skill = get_email_skill()
        parts = args_text.strip().split(None, 1)
        action = parts[0].lower() if parts else "read"
        rest = parts[1] if len(parts) > 1 else ""

        if action == "read":
            msgs = await skill.read_inbox(limit=10)
            if not msgs:
                turn.result = "收件箱为空" if zh else "Inbox is empty"
            else:
                turn.result = "\n\n---\n\n".join(
                    f"[{m.date}] {m.sender} → {m.subject}\n{m.body[:200]}"
                    for m in msgs
                )
        elif action == "send":
            turn.result = "用法: /email send <收件人> <主题> <正文>" if zh else "Usage: /email send <to> <subject> <body>"
        elif action == "search":
            msgs = await skill.search(rest)
            turn.result = "\n\n---\n\n".join(
                f"[{m.date}] {m.sender} → {m.subject}" for m in msgs
            ) if msgs else "未找到" if zh else "Not found"
        else:
            turn.result = "用法: /email read|send|search" if zh else "Usage: /email read|send|search"
        turn.record_success(turn.result, 0)

    # --------------------------------------------------- Calendar handler
    async def _handle_calendar(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在查询日程...", "calendar")
        zh = self._is_zh()
        skill = get_calendar_skill()
        parts = args_text.strip().split(None, 1)
        action = parts[0].lower() if parts else "list"

        if action == "list" or action == "today":
            events = await skill.list_today()
            turn.result = "今日日程:\n" + skill.format_events(events)
        elif action == "week":
            events = await skill.list_this_week()
            turn.result = "本周日程:\n" + skill.format_events(events)
        elif action == "create":
            turn.result = "用法: /calendar create <标题> <开始时间> [结束时间]" if zh else "Usage: /calendar create <title> <start> [end]"
        else:
            turn.result = "用法: /calendar list|today|week|create" if zh else "Usage: /calendar list|today|week|create"
        turn.record_success(turn.result, 0)

    # --------------------------------------------------- Database handler
    async def _handle_db(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在查询数据库...", "db")
        zh = self._is_zh()
        skill = get_database_skill()
        parts = args_text.strip().split(None, 1)
        action = parts[0].lower() if parts else "tables"
        rest = parts[1] if len(parts) > 1 else ""

        if action == "tables":
            result = await skill.list_tables()
            if result.get("ok"):
                tables = [r[0] for r in result.get("rows", [])]
                turn.result = "数据库表:\n" + "\n".join(f"  - {t}" for t in tables)
            else:
                turn.result = f"获取失败: {result.get('error')}"
        elif action == "query":
            result = await skill.query(sql=rest)
            turn.result = skill._format_result(result) if result.get("ok") else f"查询失败: {result.get('error')}"
        else:
            turn.result = "用法: /db tables|query <sql>" if zh else "Usage: /db tables|query <sql>"
        turn.record_success(turn.result, 0)

    # --------------------------------------------------- MCP handler
    async def _handle_mcp(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在管理 MCP 服务器...", "mcp")
        zh = self._is_zh()
        server = get_mcp_server()
        # Register current skills
        if self._skills:
            for name, skill in self._skills._skills.items():
                server.register_skill(name, skill)
        turn.result = (
            f"MCP Server 就绪。已注册 {len(server._skills)} 个工具。\n"
            f"使用: /mcp start 启动服务器"
            if zh else
            f"MCP Server ready. {len(server._skills)} tools registered.\n"
            f"Use: /mcp start to start the server"
        )
        turn.record_success(turn.result, 0)

    # --------------------------------------------------- OpenAPI handler
    async def _handle_openapi(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在加载 OpenAPI 规范...", "openapi")
        zh = self._is_zh()
        skill = get_openapi_skill()
        parts = args_text.strip().split(None, 2)
        action = parts[0].lower() if parts else "list"

        if action == "load" and len(parts) >= 2:
            result = await skill.load_from_url(name=parts[1] if len(parts) > 1 else "default", url=parts[-1])
            if result.get("ok"):
                turn.result = f"已加载: {result['title']} ({result['endpoints_count']} 端点)"
            else:
                turn.result = f"加载失败: {result.get('error')}"
        elif action == "list":
            endpoints = skill.list_endpoints("default")
            if endpoints:
                turn.result = "\n".join(f"  {e['method']} {e['path']}" for e in endpoints[:20])
            else:
                turn.result = "未加载 API。用法: /openapi load <url>" if zh else "No API loaded. Usage: /openapi load <url>"
        else:
            turn.result = "用法: /openapi load <url> | list | search <keyword>" if zh else "Usage: /openapi load <url> | list | search <keyword>"
        turn.record_success(turn.result, 0)

    # --------------------------------------------------- Agent mesh handler
    async def _handle_agent_mesh(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在启动多智能体协作...", "agent_mesh")
        zh = self._is_zh()
        if not args_text.strip():
            turn.result = (
                "用法: /mesh <复杂任务描述>\n使用多个专业Agent协作完成任务"
                if zh else "Usage: /mesh <complex task description>"
            )
            return
        if self._llm is None:
            turn.result = "[LLM not initialized]"
            return
        mesh = get_agent_mesh(self._llm, self._skills)
        self._emit_progress(turn, "多智能体协作中...", "agent_mesh")
        result = await mesh.solve(args_text.strip(), model=turn.model)
        turn.result = mesh.format_result(result)
        turn.record_success(turn.result, 0)

    # --------------------------------------------------- Workflow handler
    async def _handle_workflow(self, turn: TurnContext, args_text: str) -> None:
        zh = self._is_zh()
        if not args_text.strip():
            turn.result = (
                "用法: /workflow <JSON工作流定义>\n"
                "示例: {\"name\":\"test\",\"steps\":[{\"id\":\"s1\",\"type\":\"llm_call\",\"prompt\":\"Hello\"}]}"
                if zh else "Usage: /workflow <JSON workflow definition>"
            )
            return
        if self._llm is None:
            turn.result = "[LLM not initialized]"
            return
        import json
        try:
            workflow = json.loads(args_text.strip())
        except json.JSONDecodeError:
            turn.result = "无效的JSON格式" if zh else "Invalid JSON format"
            return
        engine = get_workflow_engine(self._llm, self._skills)
        self._emit_progress(turn, "执行工作流...", "workflow")
        try:
            result = await engine.execute(workflow)
            turn.result = f"工作流完成: {result.status.value}\n耗时: {result.total_duration_ms:.0f}ms\n步骤: {len(result.steps)}"
            turn.record_success(turn.result, 0)
        except Exception as exc:
            turn.record_failure(f"workflow execution failed: {exc}")
            turn.result = f"工作流执行失败: {exc}" if zh else f"Workflow execution failed: {exc}"

    # --------------------------------------------------- Chart handler
    async def _handle_chart(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在生成图表...", "chart")
        zh = self._is_zh()
        gen = get_chart_generator()
        if not args_text.strip():
            turn.result = (
                "用法: /chart <类型> <JSON数据>\n"
                "类型: flowchart, sequence, pie, gantt, timeline, mindmap, bar, line"
                if zh else "Usage: /chart <type> <JSON data>"
            )
            return
        parts = args_text.strip().split(None, 1)
        chart_type = parts[0]
        data = {}
        if len(parts) > 1:
            import json
            try:
                data = json.loads(parts[1])
            except json.JSONDecodeError:
                pass
        turn.result = gen.generate_mermaid(chart_type, data) if chart_type in ("flowchart", "sequence", "pie", "gantt", "timeline", "mindmap") else "不支持的图表类型"
        turn.record_success(turn.result, 0)

    # --------------------------------------------------- Branch handlers
    async def _handle_branch(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在创建分支...", "branch")
        zh = self._is_zh()
        mgr = get_branch_manager()
        branch_id = mgr.branch(turn.session_id, "", args_text.strip() or "branch")
        tree = mgr.get_tree(turn.session_id)
        turn.result = (
            f"已创建分支: {branch_id}\n" + tree.visualize()
            if zh else f"Branch created: {branch_id}\n" + tree.visualize()
        )
        turn.record_success(turn.result, 0)

    async def _handle_branch_switch(self, turn: TurnContext, args_text: str) -> None:
        self._emit_progress(turn, "正在切换分支...", "branch_switch")
        zh = self._is_zh()
        mgr = get_branch_manager()
        ok = mgr.switch_branch(turn.session_id, args_text.strip())
        if ok:
            turn.result = (
                f"已切换到分支: {args_text.strip()}"
                if zh else f"Switched to branch: {args_text.strip()}"
            )
        else:
            turn.result = (
                f"分支 '{args_text.strip()}' 不存在"
                if zh else f"Branch '{args_text.strip()}' does not exist"
            )
        turn.record_success(turn.result, 0)

    async def _handle_branch_list(self, turn: TurnContext, args_text: str = "") -> None:
        self._emit_progress(turn, "正在列出分支...", "branch_list")
        mgr = get_branch_manager()
        tree = mgr.get_tree(turn.session_id)
        branches = tree.list_branches()
        if not branches:
            turn.result = "暂无分支"
        else:
            turn.result = "分支列表:\n" + "\n".join(
                f"  {'→ ' if b['is_active'] else '  '}{b['name']}: {b['messages']} 条消息"
                for b in branches
            )
        turn.record_success(turn.result, 0)

    # --------------------------------------------------- Branch auto-tracking (Round 7)
    def _record_conversation_branch(self, turn: TurnContext) -> None:
        """Automatically record every conversation turn in the branch tree."""
        try:
            mgr = get_branch_manager()
            tree = mgr.get_tree(turn.session_id)
            tree.add_message("user", turn.input_text[:500])
            if turn.result:
                tree.add_message("assistant", turn.result[:500])
        except Exception as exc:
            logger.debug("conv_branch auto-track failed: %s", exc)

    # --------------------------------------------------- Task state tracking (Round 7 fix)
    async def _update_task_state(self, turn: TurnContext) -> None:
        """Detect and track multi-step tasks across turns.

        Uses a lightweight heuristic: if the user's input contains task indicators
        (步骤, 第一步, 先...再, plan, step 1, etc.), set an active task.
        If there's already an active task, check if it's completed.
        """
        try:
            from memory.dialog_summary import get_dialog_summarizer
            summarizer = get_dialog_summarizer()
            session_id = turn.session_id
            active = summarizer.get_active_task(session_id)

            # Check if current turn completes the active task
            if active and active.status == "in_progress":
                completion_keywords = [
                    "完成", "搞定", "做好了", "结束了", "done", "complete", "finished",
                    "最后一步", "全部做完", "全部完成",
                ]
                input_lower = turn.input_text.lower()
                if any(kw in input_lower for kw in completion_keywords):
                    completed = summarizer.complete_task(session_id)
                    logger.debug("task_state: completed task '%s'", active.name)
                    self._append_task_completion_summary(turn, completed)
                    return

                # Check if all steps are done
                if active.steps:
                    all_done = all(s.get("status") == "done" for s in active.steps)
                    if all_done:
                        completed = summarizer.complete_task(session_id)
                        logger.debug("task_state: all steps done, completed '%s'", active.name)
                        self._append_task_completion_summary(turn, completed)
                        return

            # Detect new multi-step task from user input
            task_indicators = [
                "步骤", "第一步", "第二步", "先...再", "先...然后",
                "分几步", "step 1", "step 2", "plan", "计划",
                "流程", "分步",
            ]
            input_text = turn.input_text
            is_multi_step = any(indicator in input_text for indicator in task_indicators)

            if is_multi_step and not active:
                # Use LLM to extract task name and steps (lightweight, 1 call)
                if self._llm:
                    try:
                        zh = self._is_zh()
                        # 深度审计 P2-6 修复：使用 response_format JSON 模式, 避免自由文本解析失败
                        prompt = (
                            "分析以下用户请求，提取任务名称和步骤。"
                            "输出 JSON 对象，字段：task_name (string, 简短任务名), "
                            "steps (array of string, 按顺序的步骤列表, 最多 8 个)。\n\n"
                            f"请求：{input_text[:500]}\n\n"
                            "JSON 输出："
                        ) if zh else (
                            "Analyze the following user request and extract task name and steps. "
                            "Output a JSON object with fields: task_name (string, short task name), "
                            "steps (array of string, ordered steps, max 8).\n\n"
                            f"Request: {input_text[:500]}\n\n"
                            "JSON output:"
                        )
                        resp = await self._llm.chat_completion(
                            messages=[{"role": "user", "content": prompt}],
                            model=turn.model,
                            max_tokens=400,
                            tools=None,
                            temperature=0.2,
                            response_format={"type": "json_object"},
                        )
                        text = (resp.get("text") or "").strip()
                        task_name = None
                        steps: List[str] = []
                        # 优先尝试 JSON 解析; 失败则回退到行解析
                        try:
                            import json as _json
                            # 部分模型会在 JSON 外包 ```json ... ``` 围栏, 需剥离
                            cleaned = text
                            if cleaned.startswith("```"):
                                cleaned = cleaned.split("```", 2)[1]
                                if cleaned.startswith("json"):
                                    cleaned = cleaned[4:]
                            obj = _json.loads(cleaned)
                            task_name = (obj.get("task_name") or "").strip()[:100]
                            raw_steps = obj.get("steps") or []
                            if isinstance(raw_steps, list):
                                steps = [str(s).strip()[:200] for s in raw_steps if str(s).strip()][:8]
                        except Exception:
                            # 回退: 行解析
                            lines = [l.strip() for l in text.split("\n") if l.strip()]
                            if lines:
                                task_name = lines[0][:100]
                                steps = lines[1:10] if len(lines) > 1 else []
                        if task_name:
                            summarizer.set_active_task(session_id, task_name, steps)
                            logger.info(
                                "task_state: detected task '%s' with %d steps",
                                task_name, len(steps),
                            )
                    except Exception as exc:
                        logger.debug("task_state: LLM extraction failed: %s", exc)
        except Exception as exc:
            logger.debug("task_state: update failed: %s", exc)

    def _append_task_completion_summary(self, turn: TurnContext, completed_task: Any) -> None:
        """任务完成时自动生成总结报告并追加到回复末尾。

        之前 _update_task_state 检测到任务完成只记一行 debug 日志就 return，
        用户完全不知道任务被标记完成。现在自动生成结构化总结，让 agent
        主动汇报"这个多步任务做完了，各步骤结果如何"，无需用户追问。
        """
        if completed_task is None:
            return
        try:
            zh = self._is_zh()
            name = getattr(completed_task, "name", "") or ""
            steps = getattr(completed_task, "steps", []) or []
            if not name and not steps:
                return

            if zh:
                lines = [f"\n\n---\n✅ **任务完成：{name}**"]
                if steps:
                    lines.append("各步骤回顾：")
                    for i, s in enumerate(steps):
                        step_name = s.get("step", "") if isinstance(s, dict) else str(s)
                        status = s.get("status", "") if isinstance(s, dict) else ""
                        result = s.get("result", "") if isinstance(s, dict) else ""
                        icon = {"done": "✅", "failed": "❌", "in_progress": "🔄"}.get(status, "⬜")
                        line = f"  {icon} {i+1}. {step_name}"
                        if result:
                            line += f" — {result[:80]}"
                        lines.append(line)
                lines.append("如需调整或继续，告诉我即可。")
            else:
                lines = [f"\n\n---\n✅ **Task completed: {name}**"]
                if steps:
                    lines.append("Step recap:")
                    for i, s in enumerate(steps):
                        step_name = s.get("step", "") if isinstance(s, dict) else str(s)
                        status = s.get("status", "") if isinstance(s, dict) else ""
                        result = s.get("result", "") if isinstance(s, dict) else ""
                        icon = {"done": "✅", "failed": "❌", "in_progress": "🔄"}.get(status, "⬜")
                        line = f"  {icon} {i+1}. {step_name}"
                        if result:
                            line += f" — {result[:80]}"
                        lines.append(line)
                lines.append("Let me know if you'd like to adjust or continue.")
            summary_block = "\n".join(lines)
            turn.result = (turn.result or "") + summary_block
            turn.meta["task_completion_summary"] = True
        except Exception as exc:
            logger.debug("task completion summary skipped: %s", exc)

    def _maybe_schedule_followup(self, turn: TurnContext) -> None:
        """Schedule a follow-up check task for complex/expert work.

        Round 8：接入 AsyncTaskScheduler。如果 LLM 回复中包含"稍后"/"待跟进"/
        "I'll check back" 等关键词，注册一个延迟任务。
        用户下次发起会话时，会自动加载跟进状态。

        之前 task_scheduler 完全是死代码，定义了 AsyncTaskScheduler 但
        coordinator 从未调用。现在用于真实的 follow-up 场景。
        """
        try:
            from core.task_scheduler import get_task_scheduler
            scheduler = get_task_scheduler()
        except Exception as exc:
            logger.debug("task_scheduler not available: %s", exc)
            return

        result = (turn.result or "").lower()
        followup_indicators = [
            "稍后", "稍等", "等一下", "我稍后", "我等下", "待跟进", "待完成",
            "稍后检查", "稍后回来", "我会", "我会回来", "下次",
            "later", "check back", "follow up", "followup", "will check",
        ]
        if not any(ind in result for ind in followup_indicators):
            return

        # Round 8：使用 asyncio 调度（scheduler.schedule_delayed 是 async）
        # 这里用 ensure_future fire-and-forget，不阻塞 turn 完成
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 通过 _run_coroutine_threadsafe 在后台调度
                task_args = {
                    "session_id": turn.session_id,
                    "user_input": turn.input_text[:300],
                }
                # 延迟 5 分钟（300秒），检查 session 是否需要回顾
                task_id = loop.create_task(
                    scheduler.schedule_delayed(
                        func_name="followup_check",
                        delay_seconds=300,
                        args=task_args,
                        name=f"followup-{turn.session_id[:20]}",
                    )
                )
                turn.meta["followup_scheduled"] = True
                logger.debug("followup task scheduled for session %s", turn.session_id)
        except Exception as exc:
            logger.debug("schedule_followup failed: %s", exc)

    async def _followup_check_handler(self, session_id: str = "", user_input: str = "") -> None:
        """延迟任务：跟进检查，主动给用户发消息询问是否需要继续。

        这是 AsyncTaskScheduler 调度的 followup_check 任务的实际处理函数。
        任务触发后，通过 bot_send_message 事件主动推送消息给用户，
        解决"一问一答"模式下用户不问就不说话的问题。

        Args:
            session_id: 会话 ID，格式为 "{gateway}-{chat_id}"（如 "wechat-xxx"）
            user_input: 用户原始输入，用于生成跟进提示
        """
        if not session_id:
            return

        # 从 session_id 解析 gateway 和 chat_id
        gateway = ""
        chat_id = ""
        if "-" in session_id:
            prefix, rest = session_id.split("-", 1)
            gateway_map = {
                "wechat": "wechat_personal",
                "wecom": "wecom",
                "telegram": "telegram",
                "dingtalk": "dingtalk",
                "feishu": "feishu",
                "discord": "discord",
                "slack": "slack",
                "web": "web",
                "cli": "cli",
            }
            gateway = gateway_map.get(prefix, prefix)
            chat_id = rest
        else:
            chat_id = session_id

        if not chat_id:
            return

        # 生成跟进消息
        zh = self._is_zh()
        if user_input:
            preview = user_input[:40] + "..." if len(user_input) > 40 else user_input
            if zh:
                message = f"⏰ 温馨提醒\n\n关于之前的问题「{preview}」，\n您之前提到稍后继续，现在需要我接着处理吗？\n\n回复「继续」或直接说您的需求即可。"
            else:
                message = f"⏰ Reminder\n\nRegarding your previous request「{preview}」，\nyou mentioned continuing later. Would you like to proceed now?\n\nReply 'continue' or just tell me what you need."
        else:
            if zh:
                message = "⏰ 温馨提醒\n\n您之前有任务提到稍后继续，现在需要我接着处理吗？\n\n回复「继续」或直接说您的需求即可。"
            else:
                message = "⏰ Reminder\n\nYou had a task you wanted to continue later. Would you like to proceed now?\n\nReply 'continue' or just tell me what you need."

        # 发布 bot_send_message 事件，由对应网关主动推送
        try:
            from core.events import Event
            event = Event("bot_send_message", {
                "chat_id": chat_id,
                "text": message,
                "gateway": gateway,
                "source": "coordinator:followup_check",
            })
            self.bus.publish(event)
            logger.info("coordinator: followup check sent to %s (gateway=%s)", chat_id[:8], gateway)
        except Exception as exc:
            logger.warning("coordinator: followup check send failed: %s", exc)

    # --------------------------------------------------- Advanced RAG integration (Round 7)
    async def _enhanced_web_search(self, query: str, turn: TurnContext) -> str:
        """Use advanced RAG (HyDE + rerank) to enhance web search results."""
        try:
            if self._llm is None:
                return ""
            # 修复: Coordinator 没有 self._memory, 通过 ctx 获取
            memory = getattr(self.ctx, "memory", None) if self.ctx else None
            if memory is None:
                return ""
            rag = get_advanced_rag(self._llm, memory)
            results = await rag.full_retrieval(
                query=query,
                top_k=5,
                use_hyde=True,
                use_rerank=True,
                model=turn.model,
            )
            if results:
                enhanced = []
                for i, r in enumerate(results[:5]):
                    content = str(r.get("content", r.get("text", "")))[:200]
                    enhanced.append(f"[{i+1}] {content}")
                return "\n".join(enhanced)
        except Exception as exc:
            logger.debug("advanced_rag enhanced search failed: %s", exc)
        return ""
