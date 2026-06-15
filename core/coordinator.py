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


class Coordinator(Plugin):
    """Runs the per-turn conversation loop."""

    name = "coordinator"
    depends_on = ["llm", "router", "skills"]

    def __init__(self) -> None:
        super().__init__()
        self._llm: Optional[LLMProvider] = None
        self._skills: Optional[SkillManager] = None
        self._max_tool_iterations = 5
        self._max_tokens = 2048

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

    # ------------------------------------------------------------ handlers
    async def _on_routed(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or turn.result is not None or turn.error is not None:
            return
        
        # Auto-detect language from user input
        if turn.input_text:
            from i18n import detect_language, set_language, get_language
            detected_lang = detect_language(turn.input_text)
            current_lang = get_language()
            if detected_lang != current_lang:
                set_language(detected_lang)
                logger.info(f"Auto-detected language: {detected_lang} from user input")
                # Persist language preference to config
                self._persist_language(detected_lang)
        
        # avoid double-processing — if something already published a reply,
        # skip this turn entirely
        try:
            await self._run_turn(turn)
        except Exception as exc:  # noqa: BLE001
            logger.exception("coordinator failed")
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
        # wait until turn.result is populated — small polling loop
        deadline = time.time() + 120
        while time.time() < deadline:
            if turn.result is not None or turn.error is not None:
                break
            await asyncio.sleep(0.1)

    # --------------------------------------------------------- main loop
    async def _run_turn(self, turn: TurnContext) -> None:
        if self._llm is None:
            turn.record_failure("LLM provider not bound")
            self.publish("turn_completed", turn=turn)
            return

        messages = list(turn.messages)
        # inject memory snippets (tier-2 recall) if present
        if turn.meta.get("memory_snippets"):
            mem_note = (
                "\n\nRelevant past interactions (use them to keep context):\n"
                + turn.meta["memory_snippets"]
            )
            if messages:
                messages[-1] = {"role": "user", "content": turn.input_text + mem_note}
            else:
                messages.append({"role": "user", "content": turn.input_text + mem_note})

        # pick skills for this turn (lazy loading — tier-3)
        tools: List[Dict[str, Any]] = []
        if self._skills is not None:
            chosen = self._skills.pick_relevant(turn.input_text, limit=4)
            # Always include web_search as a core capability
            web_search = self._skills.get("web_search")
            if web_search and web_search not in chosen:
                chosen.insert(0, web_search)
            turn.skills = [s.id for s in chosen]
            tools = [s.schema for s in chosen]
        else:
            turn.skills = []

        # ── Think Phase: 先想清楚再动手 ──
        # 先让 LLM 输出思考过程，拆解任务步骤、确定工具使用策略。
        # 思考内容会展示给用户，对简单问候也只需 ~1s。
        thinking_text = ""
        # Always do a quick think phase — it's cheap and provides crucial
        # context for multi-step tasks. For trivial greetings it takes ~1s.
        if True:
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
                    tools=None,  # No tools during thinking — think first, act later
                )
                thinking_text = think_resp.get("text", "").strip()
                if thinking_text:
                    # Store for frontend display
                    turn.meta["thinking"] = thinking_text
                    # Append thinking to message history as assistant message
                    messages.append({
                        "role": "assistant",
                        "content": f"[思考]\n{thinking_text}",
                    })
                    logger.debug("think phase completed (%d chars)", len(thinking_text))
            except Exception as exc:
                logger.info("think phase skipped: %s", exc)
                turn.meta["thinking"] = ""

        # ── Context compression ──
        if self.ctx and self.ctx.config:
            compression_enabled = self.ctx.config.get("router", {}).get("context_compression", {}).get("enabled", True)
            if compression_enabled:
                max_tokens = self.ctx.config.get("memory", {}).get("short_term", {}).get("max_tokens", 8000)
                # Estimate token count (rough: 1 token ≈ 4 chars)
                estimated_tokens = sum(len(str(m.get("content", ""))) // 4 for m in messages)
                if estimated_tokens > max_tokens * 0.8:
                    # Compress early messages
                    summary = await self._compress_messages(messages, turn)
                    if summary:
                        # Replace early messages with a summary system message
                        keep_recent = max(4, len(messages) // 3)  # Keep last 1/3
                        early = messages[:len(messages) - keep_recent]
                        recent = messages[len(messages) - keep_recent:]
                        messages = [
                            {"role": "system", "content": f"[对话历史摘要]\n{summary}"}
                        ] + recent
                        turn.meta["context_compressed"] = True
                        turn.meta["compressed_messages"] = len(early)

        # ── Delegation check ──
        if turn.meta.get("enable_delegation") or self._detect_complex_task(turn.input_text):
            from core.sub_agent import DelegationManager
            try:
                delegator = DelegationManager(self._llm, self._skills)
                result = await delegator.execute(turn.input_text, turn.model)
                if result.get("parallel"):
                    turn.result = result["result"]
                    turn.meta["delegation_used"] = True
                    turn.meta["subtask_count"] = len(result["subtasks"])
                    turn.meta["delegation_total_tokens"] = result["total_tokens"]
                    turn.record_success(result["result"], result.get("total_tokens", 0))

                    # Auto-extract entities for knowledge graph
                    if self.ctx and hasattr(self.ctx, 'memory') and hasattr(self.ctx.memory, '_kg') and self.ctx.memory._kg:
                        full_text = f"{turn.input_text}\n{result['result']}"
                        try:
                            count = self.ctx.memory._kg.extract_from_text(full_text, source=turn.session_id)
                            if count > 0:
                                logger.debug("Extracted %d entities from turn %s", count, turn.session_id)
                        except Exception as exc:
                            logger.debug("KG extraction failed: %s", exc)
                            pass

                    self.publish("turn_completed", turn=turn)
                    logger.info("delegation completed (%d subtasks, %d tokens, %.2fs)",
                                result.get("subtask_count", 0),
                                result.get("total_tokens", 0),
                                result.get("duration_ms", 0) / 1000)
                    return  # Skip normal tool loop
            except Exception as exc:
                logger.warning("delegation failed, falling back to normal flow: %s", exc)

        # ── Tool-call loop ──
        # force a final reply.  This mirrors the classic ReAct loop but we
        # keep it dead simple (no scratchpad, no tree of thought).
        final_text = ""
        total_tokens = 0
        _failed_skills: Dict[str, int] = {}  # Track consecutive failures per skill
        _MAX_SKILL_FAILURES = 3  # Max consecutive failures before forcing skip

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
                # final text reply — record in message history
                final_text = resp.get("text", "") or ""
                if final_text:
                    messages.append({"role": "assistant", "content": final_text})
                break

            provider = turn.model.split("/")[0] if turn.model and "/" in turn.model else "openai"

            # Append the assistant's tool call request to message history
            if provider == "anthropic":
                for idx, tc in enumerate(tool_calls):
                    name = tc.get("name") or ""
                    args = tc.get("args") or {}
                    result = await self._dispatch_smart(tc, name, args, _failed_skills)
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
                raw_tool_calls = resp.get("tool_calls_raw") or tool_calls
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": raw_tool_calls,
                })
                for tc in tool_calls:
                    name = tc.get("name") or ""
                    args = tc.get("args") or {}
                    result = await self._dispatch_smart(tc, name, args, _failed_skills)
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
            # If ALL called skills in this iteration failed, and we're past iteration 1,
            # inject a hint to nudge the model to synthesize rather than retry
            if i >= 1 and all(
                name in _failed_skills and _failed_skills[name] >= _MAX_SKILL_FAILURES
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
            # loop exhausted — force a plain text call with explicit synthesis instruction
            messages.append({
                "role": "user",
                "content": (
                    "[系统提示：工具调用已达上限。请根据你已有的知识和前面获取的信息，"
                    "直接给用户一个完整、有用的最终答复。不要提及工具不可用或搜索失败。]"
                ),
            })

            # After tool loop exhaustion: apply self-improvement
            if self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver:
                patterns = self.ctx.self_improver.analyze_patterns()
                if patterns:
                    for p in patterns[:2]:  # Apply top 2 suggestions
                        suggestion = p.get("suggestion", "")
                        if suggestion:
                            turn.meta["improvement_suggestion"] = suggestion
                            # Inject the suggestion as a system message for the next attempt
                            inject_msg = {"role": "system", "content": f"[自改进提示] {suggestion}"}
                            if inject_msg not in messages:
                                messages.insert(-2, inject_msg)

            resp = await self._llm.chat_completion(
                messages=messages, model=turn.model, max_tokens=self._max_tokens,
            )
            final_text = resp.get("text", "") or "(no reply)"
            total_tokens += int(resp.get("tokens_used") or 0)

        # After tool loop exhaustion: record failure for self-improvement
        if turn.result is None and turn.error:
            if self.ctx and hasattr(self.ctx, 'self_improver') and self.ctx.self_improver:
                error_type = turn.error if isinstance(turn.error, str) else turn.error.get("type", "unknown") if isinstance(turn.error, dict) else "unknown"
                error_detail = turn.error if isinstance(turn.error, str) else turn.error.get("detail", str(turn.error)) if isinstance(turn.error, dict) else str(turn.error)
                self.ctx.self_improver.record_failure(
                    user_input=turn.input_text,
                    error_type=error_type,
                    error_detail=error_detail,
                    turn_meta=turn.meta,
                )

        if not final_text:
            final_text = "(no reply produced)"
        turn.record_success(final_text, total_tokens)

        # Auto-extract entities for knowledge graph
        if self.ctx and hasattr(self.ctx, 'memory') and hasattr(self.ctx.memory, '_kg') and self.ctx.memory._kg:
            full_text = f"{turn.input_text}\n{final_text}"
            try:
                count = self.ctx.memory._kg.extract_from_text(full_text, source=turn.session_id)
                if count > 0:
                    logger.debug("Extracted %d entities from turn %s", count, turn.session_id)
            except Exception as exc:
                logger.debug("KG extraction failed: %s", exc)
                pass

        self.publish("turn_completed", turn=turn)
        logger.info("reply produced (%d tokens, %.2fs)",
                    turn.tokens_used, turn.duration_seconds or 0)
