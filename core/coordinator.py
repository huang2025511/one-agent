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
    ) -> str:
        """Dispatch a skill with smart failure tracking.

        If a skill has failed too many times consecutively, return a hint
        to the model to stop retrying and use its own knowledge instead.
        """
        _MAX = 3
        if failed_skills.get(name, 0) >= _MAX:
            return (
                f"[{name} 不可用（已连续失败 {_MAX} 次），"
                "请停止调用此工具，直接用你的知识给出答案。"
            )
        try:
            if self._skills is not None:
                result = await self._skills.dispatch(name, args)
                result_str = str(result)
            else:
                result_str = "[no skill manager bound]"
        except Exception as exc:  # noqa: BLE001
            logger.exception("skill dispatch failed: %s(%s)", name, args)
            result_str = f"[skill error: {exc}]"

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

        return result_str

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

        # iterative tool-call loop — supports up to N tool turns before we
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
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") or f"call_{idx}",
                        "content": str(result),
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
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") or "",
                        "name": name,
                        "content": str(result),
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
            resp = await self._llm.chat_completion(
                messages=messages, model=turn.model, max_tokens=self._max_tokens,
            )
            final_text = resp.get("text", "") or "(no reply)"
            total_tokens += int(resp.get("tokens_used") or 0)

        if not final_text:
            final_text = "(no reply produced)"
        turn.record_success(final_text, total_tokens)
        self.publish("turn_completed", turn=turn)
        logger.info("reply produced (%d tokens, %.2fs)",
                    turn.tokens_used, turn.duration_seconds or 0)
