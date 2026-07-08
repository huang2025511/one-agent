"""Unit tests for Coordinator core execution paths.

Covers the five core methods that were previously under-tested:
  - ``_tool_loop``         — tool-call loop (happy path, direct reply, max-iter)
  - ``_execute_tool_calls`` — message appending, provider branches, self-improver
  - ``_reflect_phase``      — reflection stored in turn.meta
  - ``_handle_slash_command`` — /help, /status, /clear, unknown, error paths
  - ``_dispatch_smart``     — failure tracking boundary cases

Design:
  - Uses ``StubLLM`` / ``StubSkills`` patterns (no network, no real model),
    matching the existing style in ``tests/unit_tests.py``.
  - Native ``assert`` statements only (no custom ``_check`` framework).
  - Each test is self-contained — builds its own ``Coordinator`` + stubs,
    no shared/global state.
  - ``tempfile.TemporaryDirectory`` is used whenever a real DB-backed
    component (e.g. ``SelfImprover``) is instantiated.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.context import TurnContext  # noqa: E402
from core.coordinator import MAX_SKILL_FAILURES, Coordinator  # noqa: E402
from core.tool_result import ToolResult  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════
# Stubs
# ══════════════════════════════════════════════════════════════════════════


class StubLLM:
    """Returns canned ``chat_completion`` responses in order.

    If the coordinator makes more calls than responses provided, the last
    response is repeated — this keeps tests resilient to off-by-one counts.
    """

    def __init__(self, responses: List[Dict[str, Any]]):
        self._responses = list(responses)
        self.calls = 0
        self.call_args: List[Dict[str, Any]] = []

    async def chat_completion(self, **kwargs):
        self.call_args.append(kwargs)
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


class StubSkill:
    """Minimal Skill stand-in for ``SkillManager.get``."""

    def __init__(self, id: str, schema: Optional[Dict[str, Any]] = None):
        self.id = id
        self.schema = schema or {"name": id}
        self.title = id
        self.description = ""


class StubSkills:
    """Minimal SkillManager stand-in: records dispatch calls, returns canned result."""

    def __init__(
        self,
        dispatch_result: Any = None,
        skills: Optional[Dict[str, StubSkill]] = None,
    ):
        self._dispatch_result = dispatch_result
        self._skills = skills or {}
        self.dispatch_calls: List[tuple] = []

    async def dispatch(self, name, args):
        self.dispatch_calls.append((name, args))
        if callable(self._dispatch_result):
            return self._dispatch_result(name, args)
        return self._dispatch_result

    def pick_relevant(self, text, limit=4):
        return list(self._skills.values())[:limit]

    def get(self, id):
        return self._skills.get(id)


class StubBus:
    """Captures published events for assertion."""

    def __init__(self):
        self.published: List[Dict[str, Any]] = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, *a, **kw):
        pass

    def unsubscribe(self, *a, **kw):
        pass


def _make_coordinator(
    llm: Any = None,
    skills: Any = None,
    bus: Any = None,
) -> Coordinator:
    """Build a Coordinator with stubs wired in (no setup() needed)."""
    coord = Coordinator()
    coord._llm = llm
    coord._skills = skills
    coord.bus = bus
    return coord


def _make_turn(input_text: str = "hello", model: str = "test/model") -> TurnContext:
    return TurnContext(input_text=input_text, model=model)


# ══════════════════════════════════════════════════════════════════════════
# _tool_loop
# ══════════════════════════════════════════════════════════════════════════


async def test_tool_loop_tool_call_then_final_reply():
    """LLM returns a tool call → skill executes → LLM produces final reply."""
    responses = [
        {
            "text": "",
            "tool_calls": [{"id": "c1", "name": "echo", "args": {"input": "hi"}}],
            "tokens_used": 5,
        },
        {"text": "final answer", "tool_calls": [], "tokens_used": 7},
    ]
    llm = StubLLM(responses)
    skills = StubSkills(dispatch_result="echo-result")
    coord = _make_coordinator(llm=llm, skills=skills)
    turn = _make_turn()
    messages = [{"role": "user", "content": "hello"}]

    await coord._tool_loop(messages, turn, tools=[{"name": "echo"}])

    assert turn.result == "final answer"
    assert turn.error is None
    assert llm.calls == 2
    assert len(skills.dispatch_calls) == 1
    assert skills.dispatch_calls[0][0] == "echo"
    # tool result recorded in turn meta
    tool_results = turn.meta.get("tool_results", [])
    assert len(tool_results) == 1
    assert tool_results[0].tool_name == "echo"
    # messages contain a tool-role entry and a final assistant message
    roles = [m.get("role") for m in messages]
    assert "tool" in roles
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "final answer"
    # tokens accumulated across both calls
    assert turn.tokens_used == 12


async def test_tool_loop_direct_reply_no_tools():
    """LLM replies directly with no tool calls → immediate return, single call."""
    llm = StubLLM([{"text": "hi there", "tool_calls": [], "tokens_used": 3}])
    skills = StubSkills(dispatch_result="unused")
    coord = _make_coordinator(llm=llm, skills=skills)
    turn = _make_turn()
    messages = [{"role": "user", "content": "hello"}]

    await coord._tool_loop(messages, turn, tools=[])

    assert turn.result == "hi there"
    assert llm.calls == 1
    assert skills.dispatch_calls == []
    assert turn.meta.get("tool_results", []) == []
    assert messages[-1] == {"role": "assistant", "content": "hi there"}


async def test_tool_loop_max_iterations_graceful_degradation():
    """When LLM keeps requesting tools, loop hits max → exhaustion fallback reply."""
    tool_call = [{"id": "c1", "name": "echo", "args": {"input": "x"}}]
    # First two calls return tool_calls (loop body), third is the exhaustion
    # call that produces the final fallback reply.
    responses = [
        {"text": "", "tool_calls": tool_call, "tokens_used": 2},
        {"text": "", "tool_calls": tool_call, "tokens_used": 2},
        {"text": "degraded final", "tool_calls": [], "tokens_used": 4},
    ]
    llm = StubLLM(responses)
    skills = StubSkills(dispatch_result="r")
    coord = _make_coordinator(llm=llm, skills=skills)
    coord._max_tool_iterations = 2  # small for a fast, deterministic test
    turn = _make_turn()
    messages = [{"role": "user", "content": "hello"}]

    await coord._tool_loop(messages, turn, tools=[{"name": "echo"}])

    # Loop ran _max_tool_iterations times, then one exhaustion call.
    assert llm.calls == coord._max_tool_iterations + 1
    assert turn.result == "degraded final"
    assert turn.error is None
    # The exhaustion handler injects a [系统提示] user message.
    assert any(
        "系统提示" in str(m.get("content", ""))
        for m in messages
        if m.get("role") == "user"
    )


async def test_tool_loop_empty_final_text_uses_placeholder():
    """If the LLM returns empty text with no tool calls, a placeholder is used."""
    llm = StubLLM([{"text": "", "tool_calls": [], "tokens_used": 0}])
    skills = StubSkills()
    coord = _make_coordinator(llm=llm, skills=skills)
    turn = _make_turn()
    messages = [{"role": "user", "content": "hi"}]

    await coord._tool_loop(messages, turn, tools=[])

    # Placeholder text is internationalized — just verify it's non-empty
    assert turn.result and len(turn.result) > 0


# ══════════════════════════════════════════════════════════════════════════
# _execute_tool_calls
# ══════════════════════════════════════════════════════════════════════════


async def test_execute_tool_calls_appends_tool_messages():
    """_execute_tool_calls appends the assistant tool_calls wrapper + tool results."""
    from core.tool_cache import get_tool_cache
    get_tool_cache().clear()  # 避免跨测试缓存污染
    skills = StubSkills(dispatch_result="result-data")
    coord = _make_coordinator(skills=skills)
    turn = _make_turn(model="openai/gpt-4o")  # non-anthropic provider
    messages = [{"role": "user", "content": "run echo"}]
    tool_calls = [
        {"id": "call_1", "name": "echo", "args": {"input": "hi"}},
        {"id": "call_2", "name": "calc", "args": {"expr": "1+1"}},
    ]
    failed: Dict[str, int] = {}

    await coord._execute_tool_calls(messages, turn, tool_calls, failed, iteration=0)

    # assistant message carrying tool_calls is appended
    assistant_msg = next(m for m in messages if m.get("tool_calls"))
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] is None
    # two tool-result messages appended, one per call
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert tool_msgs[0]["name"] == "echo"
    assert "result-data" in tool_msgs[0]["content"]
    # tool_results recorded in turn meta
    assert len(turn.meta["tool_results"]) == 2
    # both skills dispatched
    assert len(skills.dispatch_calls) == 2


async def test_execute_tool_calls_anthropic_provider_path():
    """Anthropic provider path skips the assistant tool_calls wrapper message."""
    skills = StubSkills(dispatch_result="ok")
    coord = _make_coordinator(skills=skills)
    turn = _make_turn(model="anthropic/claude-3")
    messages = [{"role": "user", "content": "go"}]
    tool_calls = [{"id": "c1", "name": "echo", "args": {}}]

    await coord._execute_tool_calls(messages, turn, tool_calls, {}, iteration=0)

    # anthropic branch does NOT add an assistant message with tool_calls
    assert not any(m.get("tool_calls") for m in messages)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "c1"


async def test_execute_tool_calls_negative_iteration_raises():
    """A negative iteration index is rejected with RuntimeError."""
    coord = _make_coordinator(skills=StubSkills())
    turn = _make_turn()
    with pytest.raises(RuntimeError):
        await coord._execute_tool_calls([], turn, [{}], {}, iteration=-1)


async def test_execute_tool_calls_records_unavailable_to_self_improver():
    """When a tool is unavailable and ctx has a self_improver, the failure
    is recorded for self-improvement analysis.

    Uses a real ``SelfImprover`` backed by a temp SQLite DB.
    """
    from core.self_improve import SelfImprover

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "improver.db")
        si = SelfImprover(db_path)
        try:
            # Skill dispatch must NOT be called — failed_skills already at cap.
            class UnusedSkills:
                async def dispatch(self, name, args):
                    raise AssertionError("dispatch should be skipped at max failures")

            coord = _make_coordinator(skills=UnusedSkills())
            coord.ctx = type("Ctx", (), {"self_improver": si})()
            turn = _make_turn(input_text="search the web", model="openai/gpt-4o")
            messages = [{"role": "user", "content": "go"}]
            tool_calls = [{"id": "c1", "name": "search", "args": {}}]
            failed = {"search": MAX_SKILL_FAILURES}

            await coord._execute_tool_calls(messages, turn, tool_calls, failed, iteration=0)

            stats = si.get_stats()
            assert stats["total_failures"] == 1
        finally:
            si._conn.close()


# ══════════════════════════════════════════════════════════════════════════
# _reflect_phase
# ══════════════════════════════════════════════════════════════════════════


async def test_reflect_phase_stores_reflection_in_meta():
    """_reflect_phase stores LLM reflection text in turn.meta['reflection']."""
    llm = StubLLM([{"text": "the plan has a risk in step 3", "tokens_used": 5}])
    coord = _make_coordinator(llm=llm)
    turn = _make_turn()
    turn.meta["thinking"] = "original plan"  # reflect requires prior thinking
    messages = [{"role": "user", "content": "do task"}]

    await coord._reflect_phase(messages, turn)

    assert turn.meta["reflection"] == "the plan has a risk in step 3"
    # reflection injected as an assistant message (header is i18n-dependent,
    # so assert on the reflect text itself which is language-independent)
    reflect_msgs = [
        m for m in messages
        if m.get("role") == "assistant"
        and "the plan has a risk in step 3" in str(m.get("content", ""))
    ]
    assert len(reflect_msgs) == 1
    # a follow-up user prompt is appended to continue execution
    assert messages[-1]["role"] == "user"


async def test_reflect_phase_skips_when_no_thinking():
    """If turn.meta has no 'thinking', reflect phase is a no-op (no LLM call)."""
    llm = StubLLM([])
    coord = _make_coordinator(llm=llm)
    turn = _make_turn()
    messages = [{"role": "user", "content": "hi"}]

    await coord._reflect_phase(messages, turn)

    assert llm.calls == 0
    assert "reflection" not in turn.meta
    assert len(messages) == 1  # unchanged


async def test_reflect_phase_handles_llm_exception():
    """If the LLM raises, reflect phase sets empty reflection and does not crash."""

    class FailingLLM:
        async def chat_completion(self, **kwargs):
            raise RuntimeError("llm down")

    coord = _make_coordinator(llm=FailingLLM())
    turn = _make_turn()
    turn.meta["thinking"] = "plan"
    messages = [{"role": "user", "content": "hi"}]

    await coord._reflect_phase(messages, turn)

    assert turn.meta.get("reflection") == ""


# ══════════════════════════════════════════════════════════════════════════
# _handle_slash_command
# ══════════════════════════════════════════════════════════════════════════


async def test_slash_command_help_dispatches_skill():
    """/help dispatches the 'help' skill and publishes turn_completed."""
    bus = StubBus()
    skills = StubSkills(
        dispatch_result="Help menu: ...",
        skills={"help": StubSkill("help")},
    )
    coord = _make_coordinator(skills=skills, bus=bus)
    turn = _make_turn(input_text="/help")

    handled = await coord._handle_slash_command(turn)

    assert handled is True
    assert turn.result == "Help menu: ..."
    assert len(skills.dispatch_calls) == 1
    assert skills.dispatch_calls[0][0] == "help"
    assert any(e["type"] == "turn_completed" for e in bus.published)


async def test_slash_command_status_with_args():
    """/status <text> dispatches the 'status' skill with args passed as 'input'."""
    skills = StubSkills(
        dispatch_result="all good",
        skills={"status": StubSkill("status")},
    )
    coord = _make_coordinator(skills=skills, bus=StubBus())
    turn = _make_turn(input_text="/status verbose")

    handled = await coord._handle_slash_command(turn)

    assert handled is True
    assert turn.result == "all good"
    name, args = skills.dispatch_calls[0]
    assert name == "status"
    assert args.get("input") == "verbose"


async def test_slash_command_clear_chinese_alias():
    """Chinese alias /清空 maps to the 'clear' skill."""
    skills = StubSkills(
        dispatch_result="cleared",
        skills={"clear": StubSkill("clear")},
    )
    coord = _make_coordinator(skills=skills, bus=StubBus())
    turn = _make_turn(input_text="/清空")

    handled = await coord._handle_slash_command(turn)

    assert handled is True
    assert turn.result == "cleared"
    assert skills.dispatch_calls[0][0] == "clear"


async def test_slash_command_unknown_returns_hint():
    """Unknown slash command sets a hint result and returns True."""
    bus = StubBus()
    coord = _make_coordinator(skills=StubSkills(), bus=bus)
    turn = _make_turn(input_text="/zzz-nonexistent-cmd")

    handled = await coord._handle_slash_command(turn)

    assert handled is True
    assert "未知命令" in turn.result
    assert "zzz-nonexistent-cmd" in turn.result
    assert any(e["type"] == "turn_completed" for e in bus.published)


async def test_slash_command_not_a_slash_returns_false():
    """Non-slash input is not handled — returns False, leaves turn untouched."""
    coord = _make_coordinator(skills=StubSkills(), bus=StubBus())
    turn = _make_turn(input_text="hello world")

    handled = await coord._handle_slash_command(turn)

    assert handled is False
    assert turn.result is None


async def test_slash_command_skill_missing():
    """Known command but skill not registered → '[技能不存在]' result."""
    skills = StubSkills(skills={})  # no skills registered
    coord = _make_coordinator(skills=skills, bus=StubBus())
    turn = _make_turn(input_text="/help")

    handled = await coord._handle_slash_command(turn)

    assert handled is True
    assert "技能不存在" in turn.result


async def test_slash_command_no_skills_bound():
    """When _skills is None, slash command returns '[技能系统未初始化]'."""
    coord = _make_coordinator(skills=None, bus=StubBus())
    turn = _make_turn(input_text="/help")

    handled = await coord._handle_slash_command(turn)

    assert handled is True
    assert "技能系统未初始化" in turn.result


async def test_slash_command_dispatch_exception_handled():
    """If skill dispatch raises, the error is caught and reported in result."""

    class ExplodingSkills(StubSkills):
        async def dispatch(self, name, args):
            raise RuntimeError("boom")

    skills = ExplodingSkills(skills={"help": StubSkill("help")})
    coord = _make_coordinator(skills=skills, bus=StubBus())
    turn = _make_turn(input_text="/help")

    handled = await coord._handle_slash_command(turn)

    assert handled is True
    assert "执行错误" in turn.result
    assert "boom" in turn.result


# ══════════════════════════════════════════════════════════════════════════
# _dispatch_smart — boundary cases (supplements tests/unit_tests.py)
# ══════════════════════════════════════════════════════════════════════════


async def test_dispatch_smart_no_skills_bound_returns_hint():
    """With no SkillManager bound, returns a '[no skill manager bound]' result."""
    coord = _make_coordinator(skills=None)
    failed: Dict[str, int] = {}

    result = await coord._dispatch_smart({}, "echo", {"input": "x"}, failed)

    assert isinstance(result, ToolResult)
    assert result.status == "success"
    assert "no skill manager bound" in str(result.data)
    assert failed == {}


async def test_dispatch_smart_toolresult_error_increments_counter():
    """A ToolResult with status='error' increments the failure counter."""
    class ErrorSkills:
        async def dispatch(self, name, args):
            return ToolResult(tool_name=name, status="error", error="boom")

    coord = _make_coordinator(skills=ErrorSkills())
    failed: Dict[str, int] = {}

    result = await coord._dispatch_smart({}, "search", {}, failed)

    # _dispatch_smart wraps the outcome as status='success' but tracks failure
    assert result.status == "success"
    assert failed.get("search") == 1


async def test_dispatch_smart_success_text_containing_error_does_not_increment():
    """A successful plain-string result whose text contains 'error' must NOT
    be counted as a failure — tracking is status-based, not keyword-based."""
    class TrickySkills:
        async def dispatch(self, name, args):
            return "HTTP error codes are: 404, 500"

    coord = _make_coordinator(skills=TrickySkills())
    failed = {"search": 1}  # pre-existing count from a prior real failure

    result = await coord._dispatch_smart({}, "search", {}, failed)

    assert result.status == "success"
    # success resets the counter
    assert "search" not in failed


async def test_dispatch_smart_exception_returns_error_result():
    """If skill dispatch raises, returns a ToolResult with status='error'."""
    class ExplodingSkills:
        async def dispatch(self, name, args):
            raise ValueError("network down")

    coord = _make_coordinator(skills=ExplodingSkills())
    failed: Dict[str, int] = {}

    result = await coord._dispatch_smart({}, "search", {}, failed)

    assert isinstance(result, ToolResult)
    assert result.status == "error"
    assert "network down" in result.error
    assert result.duration_ms >= 0
    # exception path does NOT touch failed_skills
    assert failed == {}


async def test_dispatch_smart_max_failures_returns_unavailable_without_dispatch():
    """Once a skill hits MAX_SKILL_FAILURES, dispatch is skipped and a
    stop-hint ToolResult is returned directly."""
    dispatch_count = [0]

    class CountingSkills:
        async def dispatch(self, name, args):
            dispatch_count[0] += 1
            return "ok"

    coord = _make_coordinator(skills=CountingSkills())
    failed = {"search": MAX_SKILL_FAILURES}

    result = await coord._dispatch_smart({}, "search", {}, failed)

    assert dispatch_count[0] == 0  # dispatch skipped entirely
    assert result.status == "unavailable"
    assert "停止调用" in result.error


async def test_dispatch_smart_success_resets_counter():
    """A success after partial failures resets the counter (key removed)."""

    class OkSkills:
        async def dispatch(self, name, args):
            return "fine"

    coord = _make_coordinator(skills=OkSkills())
    failed = {"calc": MAX_SKILL_FAILURES - 1}

    result = await coord._dispatch_smart({}, "calc", {}, failed)

    assert result.status == "success"
    assert "calc" not in failed  # counter reset


async def test_dispatch_smart_toolresult_unavailable_increments_counter():
    """A ToolResult with status='unavailable' counts as a failure."""

    class UnavailSkills:
        async def dispatch(self, name, args):
            return ToolResult(tool_name=name, status="unavailable", error="down")

    coord = _make_coordinator(skills=UnavailSkills())
    failed: Dict[str, int] = {}

    result = await coord._dispatch_smart({}, "search", {}, failed)

    assert result.status == "success"  # wrapped by _dispatch_smart
    assert failed.get("search") == 1
