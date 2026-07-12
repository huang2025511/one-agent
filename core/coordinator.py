"""The "coordinator" — wires router → LLM → skills/executors → reply.

This plugin is the single owner of the per-turn execution loop.  It
subscribes to ``turn_routed`` events, calls the LLM with the model +
messages picked by the router, optionally dispatches tool calls, and
finally publishes ``turn_completed`` so gateways can display the reply.

Keeping this separate from both the router and the LLM provider means we
can swap either without touching the control flow.

================ 开发工作流规则（每次修改前必读） ================
- Gitee 为主仓库：https://gitee.com/huang20260511/one-agent
- GitHub 为备份/APK构建仓库：https://github.com/huang2025511/one-agent
- 远程命名：origin=GitHub, gitee=Gitee
- 修改前：先运行 scripts/sync_pull.sh 从 Gitee 拉取最新代码
- 修改后：先跑测试，再运行 scripts/sync_push.sh 推送到两端
- 客户端 APK：GitHub Actions 自动构建，自动同步到 Gitee Release
- 服务端+客户端 都以 Gitee 为中心
================================================================
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

from .coordinator_helpers import (
    XML_TOOL_NAMES,
    sanitize_model_output,
    parse_xml_tool_tags,
    strip_executed_xml_tags,
    needs_web_search,
    needs_clarification_check as _needs_clarification_check_fn,
    detect_output_format,
    parse_planned_tools,
    append_to_content,
    prepend_to_content,
)
from .coordinator_features import (
    handle_chart,
    handle_branch,
    handle_branch_switch,
    handle_branch_list,
    record_conversation_branch,
    handle_email,
    handle_calendar,
    handle_db,
    handle_mcp,
    handle_openapi,
    handle_agent_mesh,
    handle_workflow,
)
from .coordinator_tasks import (
    update_task_state,
    append_task_completion_summary,
    maybe_schedule_followup,
    followup_check_handler,
)
from .coordinator_intelligence import (
    record_self_improvement_async,
    record_self_improvement,
    record_intelligence,
    extract_topics,
    generate_suggestions,
)
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
MAX_TOOL_ITERATIONS = 15  # 从5增加到15，支持更复杂的多步任务
DEFAULT_MAX_TOKENS = 2048
MAX_SKILL_FAILURES = 5  # 从3增加到5，允许更多次尝试
TURN_COMPLETION_TIMEOUT = 300.0  # 从120增加到300秒，支持更长时间的复杂任务
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
        self._bg_tasks: set = set()
        self._llm: Optional[LLMProvider] = None
        self._skills: Optional[SkillManager] = None
        self._max_tool_iterations = MAX_TOOL_ITERATIONS
        self._max_tokens = DEFAULT_MAX_TOKENS
        self._os_mode_enabled: bool = True  # 默认 OS 模式（已取消文本/OS 模式区分）
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

        已弃用：系统默认就是 OS 模式，无需切换。
        这些命令保留仅为向后兼容，统一返回弃用提示。
        """
        lang = "zh" if self._is_zh() else "en"
        if lang.startswith("zh"):
            turn.result = (
                "ℹ️ 该命令已弃用。\n\n"
                "系统默认就是 OS 模式，可直接执行系统命令（pip / npm / apt-get / curl 等），\n"
                "无需 /os-on 开启，也无需密码验证。\n\n"
                "如需执行系统命令，直接描述需求即可，或使用 /shell <命令>。"
            )
        else:
            turn.result = (
                "ℹ️ This command is deprecated.\n\n"
                "The system defaults to OS mode and can directly execute system commands "
                "(pip / npm / apt-get / curl, etc.) without /os-on or password.\n\n"
                "To run a system command, just describe your need or use /shell <command>."
            )

    async def _enable_os_mode(self, turn: TurnContext, password: str) -> bool:
        """已弃用：系统默认就是 OS 模式，直接返回 True。"""
        return True

    async def _invalidate_os_cache(self) -> None:
        """已弃用：无密码缓存可失效。"""
        return

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
                        except Exception as exc:
                            logger.debug("chart data parse failed: %s", exc)
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
            turn.result = "用法: /shell <命令>\n示例:\n  /shell ls -la\n  /shell pip install requests"
            self.publish("turn_completed", turn=turn)
            return True
        elif skill_id == "system_unlock":
            turn.result = "ℹ️ /unlock 已弃用。系统默认就是 OS 模式，所有命令可直接执行，无需密码验证。"
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
            _zh = self._is_zh()
            self._emit_progress(turn, "正在分析任务并规划工具链..." if _zh else "Analyzing task and planning tool chain...", "planning")
            if complexity >= TOOL_CHAIN_PLANNING_MIN_COMPLEXITY:
                await self._plan_tool_chain(messages, turn, tools)
            # multi-agent publishes turn_completed itself on success
            self._emit_progress(turn, "正在启动多智能体协作..." if _zh else "Starting multi-agent collaboration...", "multi_agent")
            multi_agent_done = await self._multi_agent_phase(messages, turn)
        elif complexity >= COMPLEX_COMPLEXITY_THRESHOLD:
            # Complex level: think + reflect in ONE call (performance optimization)
            _zh = self._is_zh()
            self._emit_progress(turn, "正在思考分析..." if _zh else "Thinking and analyzing...", "thinking")
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

        # 记录内部消息注入位置，用于生成结果后清理，避免 messages 膨胀
        _internal_messages_start_idx = len(messages)

        # --- Step 2.7: Task planning for complex tasks ---
        # 对复杂度 >= 0.5 的任务，先生成执行计划
        if complexity >= COMPLEX_COMPLEXITY_THRESHOLD:
            _zh = self._is_zh()
            self._emit_progress(turn, "正在规划任务步骤..." if _zh else "Planning task steps...", "planning")
            await self._task_planning(messages, turn, tools)

        # --- Step 2.8: Multi-solution comparison for expert tasks ---
        # 对复杂度 >= 0.7 的任务，生成多个解决方案并选择最优
        if complexity >= DEEP_RESEARCH_MIN_COMPLEXITY:
            self._emit_progress(turn, "正在分析多个解决方案...", "comparison")
            comparison_result = await self._multi_solution_comparison(messages, turn)
            if comparison_result:
                # 将多方案比较结果注入 messages，让后续 tool_loop 能参考最优方案
                turn.meta["multi_solution_comparison"] = comparison_result
                zh = self._is_zh()
                comparison_header = "【多方案比较结果】\n" if zh else "[Multi-solution Comparison Result]\n"
                messages.append({"role": "assistant", "content": comparison_header + comparison_result, "_internal": True})
                followup = (
                    "请参考以上多方案比较结果，选择最优方案来执行任务。"
                    if zh else
                    "Please use the best solution from the comparison above to execute the task."
                )
                messages.append({"role": "user", "content": followup, "_internal": True})

        # --- Step 2.9: Internal reasoning for complex tasks ---
        # 对复杂度 >= 0.5 的任务，在 tool_loop 之前进行多轮内部推理，
        # 让 Agent 先通过自我对话深入分析问题，再执行工具调用
        if complexity >= COMPLEX_COMPLEXITY_THRESHOLD and self._llm is not None:
            self._emit_progress(turn, "正在进行深度分析...", "reasoning")
            reasoning_result = await self._internal_reasoning_loop(messages, turn, max_rounds=3)
            if reasoning_result:
                zh = self._is_zh()
                reasoning_header = "【深度分析结论】\n" if zh else "[Deep Analysis Conclusion]\n"
                messages.append({"role": "assistant", "content": reasoning_header + reasoning_result, "_internal": True})
                followup = (
                    "请基于以上分析结论执行任务。"
                    if zh else
                    "Please execute the task based on the analysis conclusion above."
                )
                messages.append({"role": "user", "content": followup, "_internal": True})

        # --- Step 3: Tool-call loop ---
        # 只对复杂任务发进度，简单任务靠心跳兜底
        if complexity >= COMPLEX_COMPLEXITY_THRESHOLD:
            self._emit_progress(turn, "正在执行任务...", "tool_loop")
        await self._tool_loop(messages, turn, tools)

        # --- Step 3.5: Post-execution reflection ---
        # 在工具循环结束后，对复杂度 >= 0.5 的任务进行反思
        # 限制最多重新生成 1 次，防止反思-重新生成无限循环
        if complexity >= COMPLEX_COMPLEXITY_THRESHOLD and turn.result and not turn.error:
            _zh = self._is_zh()
            self._emit_progress(turn, "正在反思执行结果..." if _zh else "Reflecting on execution results...", "reflection")
            needs_regenerate = await self._post_execution_reflection(messages, turn, turn.result)
            if needs_regenerate and not turn.meta.get("_reflection_regenerated"):
                self._emit_progress(turn, "反思发现问题，重新生成..." if _zh else "Reflection found issues, regenerating...", "regeneration")
                turn.meta["_reflection_regenerated"] = True
                turn.result = None  # 重置 result，让 _tool_loop 重新生成
                await self._tool_loop(messages, turn, tools)

        # --- Step 3.6: 清理内部消息 ---
        # 删除 _internal=True 标记的消息，避免内部思考/规划/比较内容
        # 污染 conversation history 导致后续 LLM 上下文爆炸
        # 保留 system、user 原始输入和最终 assistant 回复
        _internal_count = 0
        _kept_messages: List[Dict[str, Any]] = []
        for m in messages:
            if m.pop("_internal", False):
                _internal_count += 1
            else:
                _kept_messages.append(m)
        if _internal_count > 0:
            messages.clear()
            messages.extend(_kept_messages)
            logger.debug(
                "internal messages cleaned: removed %d, kept %d",
                _internal_count, len(messages),
            )

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
        return append_to_content(content, suffix)

    @staticmethod
    def _prepend_to_content(content: Any, prefix: str) -> Any:
        return prepend_to_content(content, prefix)

    def _sanitize_model_output(self, text: str) -> str:
        return sanitize_model_output(text)

    # 已知工具名集合 — 用于校验解析出的标签名是否合法
    _XML_TOOL_NAMES = XML_TOOL_NAMES

    def _parse_xml_tool_tags(self, text: str) -> List[Dict[str, Any]]:
        return parse_xml_tool_tags(text)

    def _strip_executed_xml_tags(self, text: str) -> str:
        return strip_executed_xml_tags(text)

    def _needs_clarification_check(self, turn: TurnContext) -> bool:
        return _needs_clarification_check_fn((turn.input_text or "").strip())

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
        except Exception as exc:
            logger.debug("dialog summary injection failed: %s", exc)

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

        SkillWeaver Integration (Round 12):
        When enabled, uses semantic retrieval instead of keyword matching.
        This reduces token consumption by 99% and improves accuracy by
        aligning subtask vocabulary with tool library.

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
            # Round 12: Try SkillWeaver semantic retrieval first
            _sw_cfg = (self.ctx.config if self.ctx and self.ctx.config else {}).get("skillweaver", {})
            use_skillweaver = (
                _sw_cfg.get("enabled", True)
                and getattr(turn, "estimated_complexity", 0) >= 0.3  # Skip trivial tasks
            )
            
            if use_skillweaver:
                try:
                    from core.skillweaver import create_skillweaver_router
                    router = create_skillweaver_router(self._llm, self._skills)
                    if router.initialize():
                        # Semantic retrieval - returns skill_ids ranked by embedding similarity
                        results = router._index.retrieve(turn.input_text, top_k=6)
                        chosen = [self._skills.get(sid) for sid, _ in results if self._skills.get(sid)]
                        logger.debug(
                            "skillweaver: semantic retrieval found %d skills for '%s'",
                            len(chosen), turn.input_text[:50]
                        )
                    else:
                        chosen = self._skills.pick_relevant(turn.input_text, limit=6)
                except ImportError:
                    # Fallback to keyword matching
                    chosen = self._skills.pick_relevant(turn.input_text, limit=6)
                except Exception as exc:
                    logger.debug("skillweaver retrieval failed: %s, fallback to keywords", exc)
                    chosen = self._skills.pick_relevant(turn.input_text, limit=6)
            else:
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
                    # 实时推送思考计划到客户端
                    plan_label = "【执行计划】" if zh else "[Execution Plan]"
                    reflect_label = "【反思】" if zh else "[Reflection]"
                    self._emit_progress(turn, f"{plan_label}\n{thinking_text[:500]}", "planning")
                    self._emit_progress(turn, f"{reflect_label}\n{reflect_text[:300]}", "reflection")
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
                    # reflect 为空时也要推送 planning 进度
                    plan_label = "【执行计划】" if zh else "[Execution Plan]"
                    self._emit_progress(turn, f"{plan_label}\n{thinking_text[:500]}", "planning")
            else:
                turn.meta["thinking"] = thinking_text
                # 实时推送思考计划到客户端
                plan_label = "【执行计划】" if self._is_zh() else "[Execution Plan]"
                self._emit_progress(turn, f"{plan_label}\n{thinking_text[:500]}", "planning")

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
        return needs_web_search(text)

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
                        except Exception as exc:
                            logger.debug("RAG enhancement failed: %s", exc)

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
        """Execute tool-call loop using ReAct pattern (Reason-Act-Observe).
        
        ReAct 模式流程：
        1. 思考（Reason）: LLM 分析当前状态，决定下一步做什么
        2. 行动（Act）: 调用工具执行
        3. 观察（Observe）: 查看工具返回结果
        4. 再思考（Reason again）: 基于结果继续循环
        
        对于复杂任务，每次迭代都包含思考步骤，让 Agent 真正"思考"后再行动。
        """
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
        available_tool_names = {
            t.get("function", {}).get("name", "")
            for t in (tools or []) if isinstance(t, dict)
        }
        planned_tools = self._parse_planned_tools(
            turn.meta.get("tool_chain_plan", ""), available_tool_names,
        )
        called_tools: set = set()
        nudged: bool = False

        # Round 7 修复：熔断器 + 退避
        circuit = get_circuit_manager().get(f"llm:{provider}")
        llm_backoff_strategy = llm_backoff()
        dyn_temp = self._compute_dynamic_temperature(turn)

        # ReAct: 维护思考历史
        thinking_history: List[str] = []

        for i in range(self._max_tool_iterations):
            await rate_limiter.acquire(provider)
            dyn_temp = self._compute_dynamic_temperature(turn)

            # ========== ReAct Step 1: 思考（Reason） ==========
            # 对于复杂任务，每次迭代前都让 LLM 显式思考下一步
            complexity = getattr(turn, "estimated_complexity", 0.0)
            if complexity >= 0.3 and i > 0:
                _zh = self._is_zh()
                self._emit_progress(turn, "正在思考下一步..." if _zh else "Thinking about next step...", "thinking")
                thought_prompt = self._generate_react_thought_prompt(
                    turn, i, thinking_history, tools, _failed_skills
                )
                messages.append({"role": "user", "content": thought_prompt, "_internal": True})

                async def _do_thought_call():
                    return await self._llm.chat_completion(
                        messages=messages,
                        model=turn.model,
                        max_tokens=500,
                        temperature=dyn_temp,
                    )

                try:
                    thought_resp = await circuit.acall(
                        lambda: llm_backoff_strategy.retry(_do_thought_call),
                    )
                    thought_text = thought_resp.get("text", "") or ""
                    if thought_text:
                        thinking_history.append(thought_text)
                        # 将思考结果作为 assistant 消息加入上下文
                        # 修复：添加 _internal 标记，使其在最终清理时被移除，避免污染后续 LLM 调用
                        thought_prefix = "思考：" if _zh else "Thought: "
                        messages.append({"role": "assistant", "content": f"{thought_prefix}{thought_text}", "_internal": True})
                        # 发送完整思考内容（截断到 500 字符避免 SSE 消息过长）
                        self._emit_progress(turn, f"{thought_prefix}{thought_text[:500]}", "thinking")
                        tokens_used = int(thought_resp.get("tokens_used") or 0)
                        total_tokens += tokens_used
                        total_cost += (tokens_used / 1000) * MODEL_COST.get(turn.model, MODEL_COST.get("default", 0.002))
                except Exception as exc:
                    logger.debug("ReAct thought step failed: %s", exc)
                    # 思考失败不阻断，继续执行

            async def _do_llm_call():
                return await self._llm.chat_completion(
                    messages=messages,
                    model=turn.model,
                    max_tokens=turn.token_budget if i == 0 else self._max_tokens,
                    tools=tools or None,
                    temperature=dyn_temp,
                )

            try:
                resp = await circuit.acall(
                    lambda: llm_backoff_strategy.retry(_do_llm_call),
                )
            except CircuitOpenError:
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

            # Fallback: 解析 XML 工具标签
            if not tool_calls and resp.get("text"):
                parsed_calls = self._parse_xml_tool_tags(resp["text"])
                if parsed_calls:
                    if not (self._os_mode_enabled or turn.meta.get("needs_system_access")):
                        parsed_calls = [
                            c for c in parsed_calls if c["name"] != "system_run"
                        ]
                    if parsed_calls:
                        tool_calls = parsed_calls
                        turn.meta["xml_parsed_tool_calls"] = True
                        logger.info(
                            "tool_loop: parsed %d tool call(s) from XML tags in text output",
                            len(tool_calls),
                        )

            # ========== ReAct Step 2: 决策 — 输出最终回复或调用工具 ==========
            if not tool_calls:
                final_text = resp.get("text", "") or ""
                try:
                    if hasattr(self._llm, 'chat_completion_stream'):
                        streamed_parts = []
                        last_emit = 0
                        current_len = 0
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
                                if current_len - last_emit >= 50:
                                    last_emit = current_len
                                    self._emit_progress(
                                        turn, "".join(streamed_parts), "streaming"
                                    )
                            if chunk.get("done") and chunk.get("tokens_used"):
                                tokens_used = int(chunk["tokens_used"])
                        if streamed_parts:
                            final_text = "".join(streamed_parts)
                except Exception as stream_err:
                    logger.debug("streaming failed, using non-streamed response: %s", stream_err)

                # Gap 6：计划约束提醒
                if (
                    not nudged
                    and planned_tools
                    and i < self._max_tool_iterations - 1
                    and final_text
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
                        messages.append({"role": "user", "content": nudge, "_internal": True})
                        nudged = True
                        turn.meta["plan_nudge_triggered"] = True
                        continue

                if final_text:
                    # 检测继续思考信号
                    continue_thinking = await self._handle_continue_thinking(
                        messages, turn, final_text, tools, _failed_skills, i
                    )
                    if continue_thinking:
                        continue
                    messages.append({"role": "assistant", "content": final_text})
                break

            # ========== ReAct Step 3: 行动（Act）— 调用工具 ==========
            for tc in tool_calls:
                nm = tc.get("name") or tc.get("function", {}).get("name", "")
                if nm:
                    called_tools.add(nm)

            # Gap 5 修复：语义去重
            deduped_calls = []
            for tc in tool_calls:
                nm = tc.get("name") or tc.get("function", {}).get("name", "")
                args_str = tc.get("args") or tc.get("function", {}).get("arguments", "{}")
                if isinstance(args_str, dict):
                    args_str = str(args_str)
                dedup_key = f"{nm}:{args_str[:200]}"
                if dedup_key in called_tools:
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

            # ========== ReAct Step 4: 观察（Observe）— 分析结果并决定下一步 ==========
            # 自我修正：工具调用失败时自动分析错误并重试
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
                        recent_results = turn.meta.get("tool_results", [])
                        failed_info = ""
                        for tc in failed_in_round[:3]:
                            nm = tc.get("name") or tc.get("function", {}).get("name", "")
                            args = tc.get("args") or tc.get("function", {}).get("arguments", "{}")
                            err_msg = ""
                            for r in reversed(recent_results):
                                if getattr(r, "tool_name", "") == nm and getattr(r, "status", "") in ("error", "unavailable"):
                                    err_msg = getattr(r, "error", "") or getattr(r, "data", "")
                                    if err_msg:
                                        err_msg = err_msg[:200]
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
                        messages.append({"role": "user", "content": retry_prompt, "_internal": True})
                        turn.meta["auto_retry_count"] = retry_count + 1
                        turn.meta["auto_retry_triggered"] = True
                        logger.debug("auto-retry: triggered for %d failed tools (attempt %d)",
                                   len(failed_in_round), retry_count + 1)
                        continue

            # 动态重规划
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
                    messages.append({"role": "user", "content": replan_msg, "_internal": True})
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
            # Loop exhausted — ReAct 模式下，即使耗尽迭代次数，也要给最终回复
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

        # 记录成本信息
        turn.meta["cost"] = {
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "model": turn.model,
            "cost_per_1k": MODEL_COST.get(turn.model, MODEL_COST.get("default", 0.002)),
        }

        # 记录计划完成度
        if planned_tools:
            turn.meta["plan_completion"] = {
                "planned": planned_tools,
                "called": sorted(called_tools & set(planned_tools)),
                "missing": sorted(set(planned_tools) - called_tools),
                "nudged": nudged,
            }

        # ReAct: 记录思考历史
        if thinking_history:
            turn.meta["react_thinking"] = thinking_history

        # Record failure for self-improvement
        if turn.result is None and turn.error:
            await self._record_self_improvement_async(turn)

        if not final_text:
            final_text = (
                "抱歉，AI 未能生成回复，请重试。"
                if self._is_zh() else
                "Sorry, the AI couldn't generate a reply. Please try again."
            )

        # Gap 3 修复：输出安全扫描
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

    def _generate_react_thought_prompt(
        self, turn: TurnContext, iteration: int, thinking_history: List[str],
        tools: List[Dict[str, Any]], failed_skills: Dict[str, int],
    ) -> str:
        """生成 ReAct 思考提示，引导 LLM 在每步行动前先思考。"""
        zh = self._is_zh()
        tool_names = [t.get("function", {}).get("name", "") for t in tools]
        
        failed_info = ""
        if failed_skills:
            failed_list = [f"{name} (失败{cnt}次)" for name, cnt in failed_skills.items()]
            if zh:
                failed_info = f"\n已失败的工具：{', '.join(failed_list)}"
            else:
                failed_info = f"\nFailed tools: {', '.join(failed_list)}"
        
        if zh:
            prompt = (
                f"[思考步骤] 这是第 {iteration + 1} 轮迭代。请分析当前情况：\n"
                f"1. 已完成的操作：请总结之前的对话和工具调用结果\n"
                f"2. 当前目标：用户最初的请求是什么，现在进展到哪一步\n"
                f"3. 可用工具：{', '.join(tool_names)}{failed_info}\n"
                f"4. 下一步计划：你认为下一步应该做什么？是继续调用工具，还是可以直接回答用户？\n"
                f"5. 风险评估：如果调用工具，可能会遇到什么问题？\n\n"
                f"请用简洁的语言回答，不需要详细解释，只需说明你的思考。"
            )
        else:
            prompt = (
                f"[Thought step] This is iteration {iteration + 1}. Analyze the current situation:\n"
                f"1. Completed actions: Summarize previous conversation and tool results\n"
                f"2. Current goal: What was the user's original request and where are we now\n"
                f"3. Available tools: {', '.join(tool_names)}{failed_info}\n"
                f"4. Next step plan: What should we do next? Call another tool or answer directly?\n"
                f"5. Risk assessment: What problems might we encounter if calling tools?\n\n"
                f"Please answer concisely, just state your thinking."
            )
        
        return prompt

    # ------------------------------------------------------------ 阶段二：打破回合制
    async def _handle_continue_thinking(
        self, messages: List[Dict[str, Any]], turn: TurnContext, final_text: str,
        tools: List[Dict[str, Any]], _failed_skills: Dict[str, int], iteration: int,
    ) -> bool:
        """检测并处理 Agent 的"继续思考"信号。
        
        如果 Agent 的回复中包含特定模式（如"让我继续思考"、"让我再想想"等），
        则触发继续思考机制，让 Agent 在当前回合内继续推理，而不是直接结束。
        
        返回 True 表示继续思考，False 表示结束回合。
        """
        if not final_text:
            return False
        
        zh = self._is_zh()
        
        # 检测继续思考信号
        continue_signal_patterns_zh = [
            "让我继续思考", "让我再想想", "继续思考", "我再想想",
            "还需要进一步分析", "需要更多思考", "让我深入分析",
            "继续推理", "我需要继续", "还没完成", "还需要思考",
        ]
        continue_signal_patterns_en = [
            "let me continue thinking", "let me think more", "continue thinking",
            "need more analysis", "need to think further", "further analysis",
            "continue reasoning", "not finished yet", "need more thought",
        ]
        
        if zh:
            has_continue_signal = any(pattern in final_text for pattern in continue_signal_patterns_zh)
        else:
            has_continue_signal = any(pattern.lower() in final_text.lower() for pattern in continue_signal_patterns_en)
        
        if not has_continue_signal:
            return False
        
        # 防止误判：如果回复很长且包含详细的回答内容，continue 信号
        # 可能只是正常回答的一部分（如"建议你继续思考这个问题"）
        # 只有当回复较短（< 300 字符）或信号出现在开头/结尾时才认为是真正的信号
        text_len = len(final_text)
        if text_len > 300:
            # 对长回复，只在开头 150 或结尾 150 字符内检测
            head = final_text[:150]
            tail = final_text[-150:] if text_len > 150 else ""
            if zh:
                signal_in_short = any(pattern in head for pattern in continue_signal_patterns_zh) or \
                                  any(pattern in tail for pattern in continue_signal_patterns_zh)
            else:
                head_lower = head.lower()
                tail_lower = tail.lower()
                signal_in_short = any(pattern.lower() in head_lower for pattern in continue_signal_patterns_en) or \
                                  any(pattern.lower() in tail_lower for pattern in continue_signal_patterns_en)
            if not signal_in_short:
                return False
        
        # 触发继续思考
        if iteration >= self._max_tool_iterations - 1:
            return False  # 已经是最后一轮了
        
        logger.debug("continue_thinking: detected signal, triggering additional thought round")
        turn.meta["continue_thinking_triggered"] = True
        
        # 注入继续思考提示
        if zh:
            continue_prompt = (
                "[系统提示：你表示需要继续思考。请基于当前信息继续深入分析，"
                "可以调用工具获取更多信息，也可以进行内部推理。"
                "如果认为已经完成，请直接给出最终答案，不要再说'继续思考'。]"
            )
        else:
            continue_prompt = (
                "[System: You indicated you need to continue thinking. Please continue "
                "your analysis based on current information. You can call tools or "
                "perform internal reasoning. When you're done, give the final answer "
                "directly without saying 'continue thinking'.]"
            )
        
        messages.append({"role": "assistant", "content": final_text, "_internal": True})
        messages.append({"role": "user", "content": continue_prompt, "_internal": True})
        
        return True

    async def _internal_reasoning_loop(
        self, messages: List[Dict[str, Any]], turn: TurnContext, max_rounds: int = 5,
    ) -> str:
        """多轮内部推理循环 — Agent 可以自己跟自己对话多轮。

        在不需要调用外部工具的情况下，让 Agent 通过自我对话来深入分析问题。
        每轮推理都会基于上一轮的思考继续深入。

        返回最终的推理结果。

        修复：
        - 加单轮 LLM 调用超时控制（_INTERNAL_REASONING_TIMEOUT），防止单轮卡死拖死整个 turn
        - 加每轮输出字符数上限（_INTERNAL_REASONING_MAX_CHARS），防止 LLM 写入超大段
          污染 messages 上下文导致 OOM
        - 加总轮次时间上限，超出强制退出
        """
        if max_rounds <= 0:
            max_rounds = 3

        # 修复：单轮推理调用超时（60s），超过则跳过本轮
        _INTERNAL_REASONING_TIMEOUT = 60.0
        # 修复：每轮推理结果最大字符数（防止 LLM 输出超长结果污染 context）
        _INTERNAL_REASONING_MAX_CHARS = 4000
        # 修复：总时间上限（5分钟），超过强制退出
        _TOTAL_REASONING_BUDGET = 300.0

        provider = (turn.model or "").split("/")[0] if turn.model and "/" in (turn.model or "") else "openai"
        circuit = get_circuit_manager().get(f"llm:{provider}")
        llm_backoff_strategy = llm_backoff()
        rate_limiter = get_rate_limiter()

        zh = self._is_zh()
        reasoning_results: List[str] = []
        user_question = (turn.input_text or "")[:500]
        loop_start = time.time()

        for round_num in range(max_rounds):
            # 修复：总时间预算检查
            if time.time() - loop_start > _TOTAL_REASONING_BUDGET:
                logger.debug(
                    "internal_reasoning_loop: total budget %.1fs exceeded at round %d",
                    _TOTAL_REASONING_BUDGET, round_num,
                )
                break

            await rate_limiter.acquire(provider)

            if zh:
                reasoning_prompt = (
                    f"[内部推理第 {round_num + 1}/{max_rounds} 轮]\n"
                    f"用户问题：{user_question}\n\n"
                    "请基于之前的对话和你的知识，对这个问题进行深入分析。\n"
                    "这是你自己的内部思考过程，不需要回答用户，只需要分析问题。\n"
                    "你的分析应该越来越深入，直到找到答案或确定无法回答。\n"
                    "如果已经得出结论，请用 '结论：' 开头总结你的发现。"
                )
            else:
                reasoning_prompt = (
                    f"[Internal reasoning round {round_num + 1}/{max_rounds}]\n"
                    f"User question: {user_question}\n\n"
                    "Analyze this problem deeply based on previous conversation and your knowledge.\n"
                    "This is your internal thought process, not a reply to the user.\n"
                    "Your analysis should become progressively deeper until you find an answer.\n"
                    "If you've reached a conclusion, start with 'Conclusion:' to summarize."
                )

            messages.append({"role": "user", "content": reasoning_prompt, "_internal": True})

            if self._llm is None:
                break

            async def _do_reasoning_call():
                return await self._llm.chat_completion(
                    messages=messages,
                    model=turn.model,
                    max_tokens=1000,
                    temperature=0.7,
                )

            try:
                # 修复：单轮推理加 asyncio.wait_for 超时控制
                resp = await asyncio.wait_for(
                    circuit.acall(lambda: llm_backoff_strategy.retry(_do_reasoning_call)),
                    timeout=_INTERNAL_REASONING_TIMEOUT,
                )
                result_text = resp.get("text", "") or ""
                # 修复：限制每轮输出字符数，防止超长结果污染 context
                if len(result_text) > _INTERNAL_REASONING_MAX_CHARS:
                    trunc_label = "\n[...已截断...]" if zh else "\n[...truncated...]"
                    result_text = result_text[:_INTERNAL_REASONING_MAX_CHARS] + trunc_label
                reasoning_results.append(result_text)
                reasoning_label = f"[推理结果 {round_num + 1}]" if zh else f"[Reasoning {round_num + 1}]"
                messages.append({"role": "assistant", "content": f"{reasoning_label} {result_text}", "_internal": True})

                # 检查是否已经得出结论
                if zh and result_text.startswith("结论："):
                    break
                if not zh and result_text.startswith("Conclusion:"):
                    break

                # 发送完整推理内容（截断到 500 字符）
                preview = result_text[:500] if result_text else ""
                progress_label = f"[内部推理] 第 {round_num + 1} 轮" if zh else f"[Internal Reasoning] Round {round_num + 1}"
                self._emit_progress(turn, f"{progress_label}：{preview}" if zh else f"{progress_label}: {preview}", "thinking")

            except asyncio.TimeoutError:
                logger.debug(
                    "internal_reasoning_loop: round %d timed out after %.1fs",
                    round_num, _INTERNAL_REASONING_TIMEOUT,
                )
                break
            except Exception as exc:
                logger.debug("internal_reasoning_loop failed at round %d: %s", round_num, exc)
                break

        if reasoning_results:
            turn.meta["internal_reasoning"] = reasoning_results


        # 提取最终结论
        final_conclusion = ""
        for result in reversed(reasoning_results):
            if zh and result.startswith("结论："):
                final_conclusion = result[3:].strip()
                break
            if not zh and result.startswith("Conclusion:"):
                final_conclusion = result[11:].strip()
                break
        
        if not final_conclusion and reasoning_results:
            final_conclusion = reasoning_results[-1]
        
        return final_conclusion

    # ------------------------------------------------------------ 阶段三：任务规划与反思
    async def _task_planning(
        self, messages: List[Dict[str, Any]], turn: TurnContext, tools: List[Dict[str, Any]],
    ) -> List[str]:
        """任务规划 — 复杂任务先拆成子任务，生成执行计划。
        
        对于高复杂度任务，让 Agent 先分析任务并生成详细的执行计划，
        然后按照计划逐步执行。
        
        返回计划步骤列表。
        """
        complexity = getattr(turn, "estimated_complexity", 0.0)
        if complexity < 0.5:
            return []  # 简单任务不需要规划
        
        provider = (turn.model or "").split("/")[0] if turn.model and "/" in (turn.model or "") else "openai"
        circuit = get_circuit_manager().get(f"llm:{provider}")
        llm_backoff_strategy = llm_backoff()
        rate_limiter = get_rate_limiter()
        
        await rate_limiter.acquire(provider)
        
        zh = self._is_zh()
        tool_names = [t.get("function", {}).get("name", "") for t in tools]
        
        user_question = (turn.input_text or "")[:500]
        if zh:
            planning_prompt = (
                f"[任务规划]\n"
                f"用户请求：{user_question}\n\n"
                "请分析上述用户请求，并将其拆解为具体的执行步骤。\n"
                "每个步骤应该是一个可以独立执行的子任务。\n"
                "可用工具：" + ", ".join(tool_names) + "\n\n"
                "请按照以下格式输出计划：\n"
                "步骤 1：[子任务描述]\n"
                "步骤 2：[子任务描述]\n"
                "...\n"
                "步骤 N：[子任务描述]\n\n"
                "请确保计划完整、可行，并且步骤之间有逻辑顺序。"
            )
        else:
            planning_prompt = (
                f"[Task Planning]\n"
                f"User request: {user_question}\n\n"
                "Analyze the above user request and break it down into specific execution steps.\n"
                "Each step should be an independently executable subtask.\n"
                "Available tools: " + ", ".join(tool_names) + "\n\n"
                "Please output the plan in the following format:\n"
                "Step 1: [subtask description]\n"
                "Step 2: [subtask description]\n"
                "...\n"
                "Step N: [subtask description]\n\n"
                "Ensure the plan is complete, feasible, and has logical order between steps."
            )
        
        messages.append({"role": "user", "content": planning_prompt, "_internal": True})
        
        if self._llm is None:
            return []
        
        async def _do_planning_call():
            return await self._llm.chat_completion(
                messages=messages,
                model=turn.model,
                max_tokens=1000,
                temperature=0.7,
            )
        
        try:
            # 修复：加超时控制
            resp = await asyncio.wait_for(
                circuit.acall(lambda: llm_backoff_strategy.retry(_do_planning_call)),
                timeout=60.0,
            )
            plan_text = resp.get("text", "") or ""

            # 修复：限制 plan_text 长度，防止 LLM 输出超大段污染 context
            _PLAN_TEXT_MAX = 8000
            if len(plan_text) > _PLAN_TEXT_MAX:
                plan_text = plan_text[:_PLAN_TEXT_MAX] + "\n[...已截断...]"

            # 解析计划步骤
            steps = []
            # 修复：单步字符数上限（防止 LLM 写长篇大论）
            _STEP_TEXT_MAX = 500
            # 修复：最大步骤数（防止 LLM 输出几千步骤撑爆 messages）
            _MAX_PLAN_STEPS = 20
            if zh:
                for line in plan_text.split("\n"):
                    if len(steps) >= _MAX_PLAN_STEPS:
                        logger.debug("task_planning: hit max steps cap %d, truncating", _MAX_PLAN_STEPS)
                        break
                    line = line.strip()
                    if line.startswith("步骤") or line.startswith("Step"):
                        # 提取步骤内容 — 修复：使用 (?:步骤|Step) 而非 [步骤|Step]
                        # [步骤|Step] 是字符类，只匹配单个字符，不是匹配"步骤"或"Step"这个词
                        match = re.search(r'(?:步骤|Step)\s*\d+[：:]?\s*(.*)', line, re.IGNORECASE)
                        if match:
                            step_text = match.group(1).strip()
                            if len(step_text) > _STEP_TEXT_MAX:
                                step_text = step_text[:_STEP_TEXT_MAX] + "..."
                            steps.append(step_text)
            else:
                for line in plan_text.split("\n"):
                    if len(steps) >= _MAX_PLAN_STEPS:
                        logger.debug("task_planning: hit max steps cap %d, truncating", _MAX_PLAN_STEPS)
                        break
                    line = line.strip()
                    if line.startswith("Step") or line.startswith("step"):
                        match = re.search(r'Step\s*\d+[.:]?\s*(.*)', line, re.IGNORECASE)
                        if match:
                            step_text = match.group(1).strip()
                            if len(step_text) > _STEP_TEXT_MAX:
                                step_text = step_text[:_STEP_TEXT_MAX] + "..."
                            steps.append(step_text)

            if steps:
                turn.meta["task_plan"] = steps
                plan_msg = f"已生成 {len(steps)} 步执行计划" if zh else f"Generated {len(steps)}-step execution plan"
                self._emit_progress(turn, plan_msg, "planning")
                logger.debug("task_planning: generated %d steps", len(steps))

                # 将计划注入上下文
                if zh:
                    plan_summary = "任务计划：\n" + "\n".join([f"{i+1}. {step}" for i, step in enumerate(steps)])
                else:
                    plan_summary = "Task Plan:\n" + "\n".join([f"{i+1}. {step}" for i, step in enumerate(steps)])
                messages.append({"role": "assistant", "content": plan_summary})

            return steps

        except asyncio.TimeoutError:
            logger.debug("task_planning: LLM call timed out after 60s")
            return []
        except Exception as exc:
            logger.debug("task_planning failed: %s", exc)
            return []

    async def _post_execution_reflection(
        self, messages: List[Dict[str, Any]], turn: TurnContext, result_text: str,
    ) -> bool:
        """执行后反思 — 做完后检查结果，不对就重来。
        
        在生成最终回复后，让 Agent 反思自己的回答是否正确、完整、符合用户需求。
        如果发现问题，返回 True 表示需要重新生成。
        
        返回 True 表示需要重新生成，False 表示结果可以接受。
        """
        complexity = getattr(turn, "estimated_complexity", 0.0)
        if complexity < 0.5:
            return False  # 简单任务不需要反思
        
        if not result_text:
            return False
        
        provider = (turn.model or "").split("/")[0] if turn.model and "/" in (turn.model or "") else "openai"
        circuit = get_circuit_manager().get(f"llm:{provider}")
        llm_backoff_strategy = llm_backoff()
        rate_limiter = get_rate_limiter()
        
        await rate_limiter.acquire(provider)
        
        zh = self._is_zh()
        
        if zh:
            reflection_prompt = (
                "[执行后反思]\n"
                "请检查你刚刚给出的回答，判断是否满足以下条件：\n"
                "1. 准确性：回答是否准确？有没有事实错误？\n"
                "2. 完整性：是否回答了用户的所有问题？有没有遗漏？\n"
                "3. 相关性：回答是否与用户的问题直接相关？\n"
                "4. 逻辑性：推理过程是否清晰合理？\n"
                "5. 有用性：用户能否从回答中获得有用的信息？\n\n"
                "你的回答：\n" + result_text[:2000] + "\n\n"
                "请用 '是' 或 '否' 回答：这个回答是否需要改进？\n"
                "如果需要改进，请简要说明问题所在。"
            )
        else:
            reflection_prompt = (
                "[Post-execution Reflection]\n"
                "Please review your answer and check if it meets the following criteria:\n"
                "1. Accuracy: Is the answer accurate? Any factual errors?\n"
                "2. Completeness: Does it answer all parts of the user's question?\n"
                "3. Relevance: Is the answer directly relevant to the user's question?\n"
                "4. Logic: Is the reasoning clear and logical?\n"
                "5. Helpfulness: Can the user get useful information from the answer?\n\n"
                "Your answer:\n" + result_text[:2000] + "\n\n"
                "Please answer 'Yes' or 'No': Does this answer need improvement?\n"
                "If yes, briefly explain what's wrong."
            )
        
        messages.append({"role": "user", "content": reflection_prompt, "_internal": True})
        
        if self._llm is None:
            return False
        
        async def _do_reflection_call():
            return await self._llm.chat_completion(
                messages=messages,
                model=turn.model,
                max_tokens=500,
                temperature=0.3,
            )
        
        try:
            resp = await circuit.acall(
                lambda: llm_backoff_strategy.retry(_do_reflection_call),
            )
            reflection_result = resp.get("text", "") or ""
            
            # 精确检测是否需要重新生成 — 只检查回答的首行/首词
            # 避免 "是" 出现在 "这个回答是准确的" 中导致误判
            if zh:
                first_line = reflection_result.strip().split("\n")[0].strip()
                if first_line.startswith("否"):
                    needs_improvement = False
                else:
                    needs_improvement = first_line.startswith("是") or first_line.startswith("需要")
            else:
                first_line = reflection_result.strip().split("\n")[0].strip().lower()
                if first_line.startswith("no"):
                    needs_improvement = False
                else:
                    needs_improvement = first_line.startswith("yes") or first_line.startswith("need")
            
            if needs_improvement:
                turn.meta["reflection_needed"] = True
                turn.meta["reflection_reason"] = reflection_result
                self._emit_progress(turn, "反思发现问题，正在重新生成..." if zh else "Reflection found issues, regenerating...", "reflection")
                logger.debug("post_execution_reflection: needs improvement - %s", reflection_result)
                
                # 注入改进提示
                if zh:
                    improvement_prompt = (
                        "[系统提示：反思发现你的回答有问题：" + reflection_result[:200] + "\n"
                        "请重新生成回答，修正上述问题。]"
                    )
                else:
                    improvement_prompt = (
                        "[System: Reflection found issues: " + reflection_result[:200] + "\n"
                        "Please regenerate your answer to fix these issues.]"
                    )
                
                messages.append({"role": "assistant", "content": result_text, "_internal": True})
                messages.append({"role": "user", "content": improvement_prompt, "_internal": True})
                
                return True
            
            turn.meta["reflection_completed"] = True
            return False
            
        except Exception as exc:
            logger.debug("post_execution_reflection failed: %s", exc)
            return False

    async def _multi_solution_comparison(
        self, messages: List[Dict[str, Any]], turn: TurnContext, max_solutions: int = 3,
    ) -> str:
        """多方案比较 — 对重要问题生成多个方案，选最优。
        
        对于复杂决策类问题，让 Agent 生成多个解决方案，然后比较各个方案的优缺点，
        最后选择最优方案。
        
        返回最优方案的描述。
        """
        complexity = getattr(turn, "estimated_complexity", 0.0)
        if complexity < 0.7:
            return ""  # 不太复杂的问题不需要多方案比较
        
        provider = (turn.model or "").split("/")[0] if turn.model and "/" in (turn.model or "") else "openai"
        circuit = get_circuit_manager().get(f"llm:{provider}")
        llm_backoff_strategy = llm_backoff()
        rate_limiter = get_rate_limiter()
        
        await rate_limiter.acquire(provider)
        
        zh = self._is_zh()
        user_question = (turn.input_text or "")[:500]
        
        if zh:
            comparison_prompt = (
                f"[多方案比较]\n"
                f"用户问题：{user_question}\n\n"
                f"这是一个复杂的决策问题，请生成 {max_solutions} 个不同的解决方案。\n"
                "然后比较各个方案的优缺点，最后选择最优方案。\n\n"
                "请按照以下格式输出：\n"
                "方案 1：[方案描述]\n"
                "  优点：[列出优点]\n"
                "  缺点：[列出缺点]\n\n"
                "...\n\n"
                "最优方案：[方案编号]\n"
                "选择理由：[说明为什么选这个方案]"
            )
        else:
            comparison_prompt = (
                f"[Multi-solution Comparison]\n"
                f"User question: {user_question}\n\n"
                f"This is a complex decision problem. Please generate {max_solutions} different solutions.\n"
                "Then compare the pros and cons of each, and select the best one.\n\n"
                "Please output in the following format:\n"
                "Solution 1: [description]\n"
                "  Pros: [list pros]\n"
                "  Cons: [list cons]\n\n"
                "...\n\n"
                "Best Solution: [solution number]\n"
                "Reason: [explain why this solution was chosen]"
            )
        
        messages.append({"role": "user", "content": comparison_prompt, "_internal": True})
        
        if self._llm is None:
            return ""
        
        async def _do_comparison_call():
            return await self._llm.chat_completion(
                messages=messages,
                model=turn.model,
                max_tokens=2000,
                temperature=0.7,
            )
        
        try:
            resp = await circuit.acall(
                lambda: llm_backoff_strategy.retry(_do_comparison_call),
            )
            comparison_text = resp.get("text", "") or ""
            
            if comparison_text:
                turn.meta["multi_solution_comparison"] = comparison_text
                self._emit_progress(turn, "已完成多方案比较", "comparison")
                logger.debug("multi_solution_comparison: completed")
            
            return comparison_text
            
        except Exception as exc:
            logger.debug("multi_solution_comparison failed: %s", exc)
            return ""

    def _parse_planned_tools(
        self, plan_text: str, available_tool_names: set,
    ) -> List[str]:
        return parse_planned_tools(plan_text, available_tool_names)

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
                # 工具执行结果实时反馈：让用户知道工具执行成功还是失败
                # 之前只在 complex 任务发"正在调用"，不告知结果，用户觉得"卡住了"
                zh = self._is_zh()
                if result.status == "success":
                    data_preview = str(result.data or "")[:80]
                    self._emit_progress(
                        turn,
                        f"✅ {name} 执行成功" + (f": {data_preview}" if data_preview else ""),
                        "tool_result",
                    )
                else:
                    err_msg = (result.error or "未知错误")[:80]
                    self._emit_progress(
                        turn,
                        f"❌ {name} 失败: {err_msg}",
                        "tool_result",
                    )
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
                # 工具执行结果实时反馈：让用户知道工具执行成功还是失败
                # 之前只在 complex 任务发"正在调用"，不告知结果，用户觉得"卡住了"
                zh = self._is_zh()
                if result.status == "success":
                    data_preview = str(result.data or "")[:80]
                    self._emit_progress(
                        turn,
                        f"✅ {name} 执行成功" + (f": {data_preview}" if data_preview else ""),
                        "tool_result",
                    )
                else:
                    err_msg = (result.error or "未知错误")[:80]
                    self._emit_progress(
                        turn,
                        f"❌ {name} 失败: {err_msg}",
                        "tool_result",
                    )
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
        await record_self_improvement_async(self, turn)

    def _record_self_improvement(self, turn: TurnContext) -> None:
        record_self_improvement(self, turn)

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
        return detect_output_format(answer)

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
            except Exception as exc:
                logger.debug("style adapter setup failed: %s", exc)
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
        await record_intelligence(self, turn)

    def _extract_topics(self, text: str) -> List[str]:
        return extract_topics(text)

    async def _generate_suggestions(self, turn: TurnContext) -> List[Dict[str, Any]]:
        return await generate_suggestions(self, turn)

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
        await handle_email(self, turn, args_text)

    # --------------------------------------------------- Calendar handler
    async def _handle_calendar(self, turn: TurnContext, args_text: str) -> None:
        await handle_calendar(self, turn, args_text)

    # --------------------------------------------------- Database handler
    async def _handle_db(self, turn: TurnContext, args_text: str) -> None:
        await handle_db(self, turn, args_text)

    # --------------------------------------------------- MCP handler
    async def _handle_mcp(self, turn: TurnContext, args_text: str) -> None:
        await handle_mcp(self, turn, args_text)

    # --------------------------------------------------- OpenAPI handler
    async def _handle_openapi(self, turn: TurnContext, args_text: str) -> None:
        await handle_openapi(self, turn, args_text)

    # --------------------------------------------------- Agent mesh handler
    async def _handle_agent_mesh(self, turn: TurnContext, args_text: str) -> None:
        await handle_agent_mesh(self, turn, args_text)

    # --------------------------------------------------- Workflow handler
    async def _handle_workflow(self, turn: TurnContext, args_text: str) -> None:
        await handle_workflow(self, turn, args_text)

    # --------------------------------------------------- Chart handler
    async def _handle_chart(self, turn: TurnContext, args_text: str) -> None:
        await handle_chart(self, turn, args_text)

    # --------------------------------------------------- Branch handlers
    async def _handle_branch(self, turn: TurnContext, args_text: str) -> None:
        await handle_branch(self, turn, args_text)

    async def _handle_branch_switch(self, turn: TurnContext, args_text: str) -> None:
        await handle_branch_switch(self, turn, args_text)

    async def _handle_branch_list(self, turn: TurnContext, args_text: str = "") -> None:
        await handle_branch_list(self, turn, args_text)

    # --------------------------------------------------- Branch auto-tracking (Round 7)
    def _record_conversation_branch(self, turn: TurnContext) -> None:
        record_conversation_branch(self, turn)

    # --------------------------------------------------- Task state tracking (Round 7 fix)
    async def _update_task_state(self, turn: TurnContext) -> None:
        await update_task_state(self, turn)

    def _append_task_completion_summary(self, turn: TurnContext, completed_task: Any) -> None:
        append_task_completion_summary(self, turn, completed_task)

    def _maybe_schedule_followup(self, turn: TurnContext) -> None:
        maybe_schedule_followup(self, turn)

    async def _followup_check_handler(self, session_id: str = "", user_input: str = "") -> None:
        await followup_check_handler(self, session_id, user_input)

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
