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
        _MAX = 3
        if failed_skills.get(name, 0) >= _MAX:
            return ToolResult(
                tool_name=name,
                status="unavailable",
                error=f"已连续失败 {_MAX} 次，请停止调用此工具，直接用你的知识给出答案。",
            )
        start = time.time()
        try:
            if self._skills is not None:
                result = await self._skills.dispatch(name, args)
                result_str = str(result)
            else:
                result_str = "[no skill manager bound]"
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

        # Track failures — if result contains error keywords, count it
        if "error" in result_str.lower() or "不可用" in result_str or "unavailable" in result_str.lower():
            failed_skills[name] = failed_skills.get(name, 0) + 1
            logger.info("skill %s failed (%d/%d)", name, failed_skills[name], _MAX)
            # If just hit the limit, enrich the result with a stop hint
            if failed_skills[name] >= _MAX:
                result_str = (
                    f"[{name} 连续失败 {_MAX} 次，已标记为不可用。"
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
        except Exception:
            return ""

    @staticmethod
    def _detect_complex_task(text: str) -> bool:
        """Quick heuristic: tasks with comparison, research, or analysis keywords."""
        keywords = ["比较", "对比", "分析", "研究", "评估", "调查", "分别", "各",
                    "compare", "analyze", "research", "evaluate", "both", "each"]
        return len(text) > 50 and any(k in text for k in keywords)

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
            args["input"] = args_text
        
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
            from i18n import detect_language, set_language, get_language
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
        
        # Event-driven wait: subscribe to turn_completed for this specific turn
        # instead of polling with asyncio.sleep
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
        finally:
            self.bus.unsubscribe("turn_completed", _on_turn_completed)

    # --------------------------------------------------------- main loop
    async def _run_turn(self, turn: TurnContext) -> None:
        """Execute a single turn: think → compress → delegate/tool-loop → reply."""
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

        # Think phase
        await self._think_phase(messages, turn)

        # Context compression
        await self._compress_context(messages, turn)

        # Delegation check
        if await self._try_delegation(turn, messages):
            return

        # Tool-call loop
        await self._tool_loop(messages, turn, tools)

        # Auto-extract entities
        self._extract_entities(turn)

        self.publish("turn_completed", turn=turn)
        logger.info("reply produced (%d tokens, %.2fs)",
                    turn.tokens_used, turn.duration_seconds or 0)

    def _prepare_messages(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Prepare message list with memory snippets if present."""
        if turn is None:
            raise RuntimeError("turn cannot be None")
        if turn.input_text is None:
            raise RuntimeError("turn.input_text cannot be None")
        
        messages = list(turn.messages)
        if turn.meta.get("memory_snippets"):
            mem_note = (
                "\n\nRelevant past interactions (use them to keep context):\n"
                + turn.meta["memory_snippets"]
            )
            if messages:
                messages[-1] = {"role": "user", "content": turn.input_text + mem_note}
            else:
                messages.append({"role": "user", "content": turn.input_text + mem_note})
        return messages

    def _prepare_tools(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Pick relevant skills and prepare tool schemas."""
        if turn is None:
            raise RuntimeError("turn cannot be None")
        
        tools: List[Dict[str, Any]] = []
        if self._skills is not None:
            chosen = self._skills.pick_relevant(turn.input_text, limit=4)
            web_search = self._skills.get("web_search")
            if web_search and web_search not in chosen:
                chosen.insert(0, web_search)
            turn.skills = [s.id for s in chosen]
            tools = [s.schema for s in chosen]
        else:
            turn.skills = []
        return tools

    async def _think_phase(self, messages: List[Dict[str, Any]], turn: TurnContext) -> None:
        """Execute thinking phase to plan approach."""
        if messages is None:
            raise RuntimeError("messages cannot be None")
        if turn is None:
            raise RuntimeError("turn cannot be None")
        
        think_prompt = (
            "[思考阶段] 在动手之前，请用 2-4 句话快速思考：\n"
            "1. 用户真正要什么？（一句话）\n"
            "2. 需要分几步？用什么工具？\n"
            "3. 先执行，不要在这步就给出最终答案，思考完直接开始调用工具。"
        )
        try:
            think_resp = await self._llm.chat_completion(
                messages=messages + [{"role": "user", "content": think_prompt}],
                model=turn.model,
                max_tokens=min(turn.token_budget, 512),
                tools=None,
            )
            thinking_text = think_resp.get("text", "").strip()
            if thinking_text:
                turn.meta["thinking"] = thinking_text
                messages.append({
                    "role": "assistant",
                    "content": f"[思考]\n{thinking_text}",
                })
                logger.debug("think phase completed (%d chars)", len(thinking_text))
        except Exception as exc:
            logger.warning("think phase skipped: %s", exc)
            turn.meta["thinking"] = ""

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
        """Auto-extract entities from turn for knowledge graph."""
        assert turn is not None, "turn cannot be None"
        
        if not (self.ctx and hasattr(self.ctx, 'memory') and hasattr(self.ctx.memory, '_kg') and self.ctx.memory._kg):
            return

        final_text = turn.result or ""
        full_text = f"{turn.input_text}\n{final_text}"

        try:
            count = self.ctx.memory._kg.extract_from_text(full_text, source=turn.session_id)
            if count > 0:
                logger.debug("Extracted %d entities from turn %s", count, turn.session_id)
        except Exception as exc:
            logger.debug("KG extraction failed: %s", exc)
