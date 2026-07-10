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


# ══════════════════════════════════════════════════════════════════════════
# Pure / stateless helper methods — safe to test in isolation
# ══════════════════════════════════════════════════════════════════════════


# --- _is_zh ---

def test_is_zh_returns_true_for_chinese():
    """_is_zh returns True when i18n language starts with 'zh'."""
    from i18n import set_language
    set_language("zh")
    assert Coordinator._is_zh() is True


def test_is_zh_returns_false_for_english():
    """_is_zh returns False when language is 'en'."""
    from i18n import set_language
    set_language("en")
    assert Coordinator._is_zh() is False


# --- _sanitize_model_output ---

def test_sanitize_removes_invoke_blocks():
    """<invoke name="...">...</invoke> blocks are stripped."""
    coord = _make_coordinator()
    text = 'Before <invoke name="web_search"><parameter name="query">test</parameter>result</invoke> After'
    result = coord._sanitize_model_output(text)
    assert "<invoke" not in result
    assert "Before" in result
    assert "After" in result


def test_sanitize_removes_tool_call_blocks():
    """<tool_call>...</tool_call> blocks are stripped."""
    coord = _make_coordinator()
    text = 'Answer <tool_call>{"name":"calc"}</tool_call> done'
    result = coord._sanitize_model_output(text)
    assert "<tool_call" not in result
    assert "Answer" in result


def test_sanitize_removes_function_call_blocks():
    """<function_call>...</function_call> blocks are stripped."""
    coord = _make_coordinator()
    text = '<function_call name="web_search">test</function_call>result'
    result = coord._sanitize_model_output(text)
    assert "<function_call" not in result


def test_sanitize_preserves_plain_text():
    """Text without XML tags is returned unchanged (except strip)."""
    coord = _make_coordinator()
    text = "This is a normal response with no tags."
    assert coord._sanitize_model_output(text) == text


def test_sanitize_collapses_excessive_blank_lines():
    """3+ consecutive newlines are collapsed to 2."""
    coord = _make_coordinator()
    text = "line1\n\n\n\n\nline2"
    result = coord._sanitize_model_output(text)
    assert "\n\n\n" not in result


# --- _parse_xml_tool_tags ---

def test_parse_xml_self_closing_tag():
    """Self-closing XML tag <web_search query="..."/> is parsed into tool_calls.

    Note: _parse_xml_tool_tags maps 'query' → 'input' for web_search.
    """
    coord = _make_coordinator()
    text = 'Let me search <web_search query="python 3.13" /> for that.'
    calls = coord._parse_xml_tool_tags(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "web_search"
    assert calls[0]["args"]["input"] == "python 3.13"


def test_parse_xml_paired_tag():
    """Paired XML tags <calc expr="..."></calc> are parsed.

    Note: _parse_xml_tool_tags maps 'expr' → 'input' for calc.
    """
    coord = _make_coordinator()
    text = 'Result: <calc expr="1+1"></calc> done'
    calls = coord._parse_xml_tool_tags(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "calc"
    assert calls[0]["args"]["input"] == "1+1"


def test_parse_xml_multiple_tags():
    """Multiple XML tags in one response are all parsed."""
    coord = _make_coordinator()
    text = '<web_search query="a" /> then <calc expr="2+2" />'
    calls = coord._parse_xml_tool_tags(text)
    assert len(calls) == 2
    assert calls[0]["name"] == "web_search"
    assert calls[1]["name"] == "calc"


def test_parse_xml_unknown_tool_ignored():
    """Tags with names not in _XML_TOOL_NAMES are ignored."""
    coord = _make_coordinator()
    text = '<unknown_tool foo="bar" />'
    calls = coord._parse_xml_tool_tags(text)
    assert len(calls) == 0


def test_parse_xml_no_tags_returns_empty():
    """Text without XML tags returns empty list."""
    coord = _make_coordinator()
    assert coord._parse_xml_tool_tags("just plain text") == []


# --- _strip_executed_xml_tags ---

def test_strip_xml_removes_self_closing_tags():
    """Self-closing known tool tags are removed from text."""
    coord = _make_coordinator()
    text = 'Before <web_search query="test" /> After'
    result = coord._strip_executed_xml_tags(text)
    assert "<web_search" not in result
    assert "Before" in result
    assert "After" in result


def test_strip_xml_preserves_unknown_tags():
    """Non-tool XML tags are preserved."""
    coord = _make_coordinator()
    text = '<custom_tag>content</custom_tag>'
    result = coord._strip_executed_xml_tags(text)
    assert "<custom_tag>" in result


def test_strip_xml_empty_string():
    """Empty string is handled without error."""
    coord = _make_coordinator()
    assert coord._strip_executed_xml_tags("") == ""


# --- _needs_clarification_check ---

def test_clarification_short_input_returns_false():
    """Short inputs (< 15 chars) don't need clarification."""
    coord = _make_coordinator()
    turn = _make_turn(input_text="hello")
    assert coord._needs_clarification_check(turn) is False


def test_clarification_code_input_returns_false():
    """Inputs with code blocks don't need clarification."""
    coord = _make_coordinator()
    turn = _make_turn(input_text="```python\nprint('hello world')\n```")
    assert coord._needs_clarification_check(turn) is False


def test_clarification_url_input_returns_false():
    """Inputs with URLs don't need clarification."""
    coord = _make_coordinator()
    turn = _make_turn(input_text="please visit https://example.com for details")
    assert coord._needs_clarification_check(turn) is False


def test_clarification_long_ambiguous_returns_true():
    """Long inputs without markers return True."""
    coord = _make_coordinator()
    turn = _make_turn(input_text="can you help me understand the situation")
    assert coord._needs_clarification_check(turn) is True


# --- _needs_web_search ---

def test_web_search_chinese_keywords():
    """Chinese search keywords trigger True."""
    coord = _make_coordinator()
    assert coord._needs_web_search("搜索一下最新的新闻") is True
    assert coord._needs_web_search("今天的天气怎么样") is True


def test_web_search_english_keywords():
    """English search keywords trigger True."""
    coord = _make_coordinator()
    assert coord._needs_web_search("search for the latest news") is True
    assert coord._needs_web_search("what's the weather today") is True


def test_web_search_no_keywords():
    """Non-search inputs return False."""
    coord = _make_coordinator()
    assert coord._needs_web_search("write a hello world program") is False
    assert coord._needs_web_search("calculate 1+1") is False


def test_web_search_year_keyword_long():
    """Year + long text triggers search."""
    coord = _make_coordinator()
    assert coord._needs_web_search("what happened in 2025 that was important") is True


# --- _detect_output_format ---

def test_detect_format_code_with_def():
    """Code blocks with def/class/import are detected as code format."""
    coord = _make_coordinator()
    fmt = coord._detect_output_format("Here:\n```python\ndef hello():\n    pass\n```")
    assert "代码块" in fmt


def test_detect_format_table():
    """Markdown tables are detected."""
    coord = _make_coordinator()
    fmt = coord._detect_output_format("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "表格" in fmt


def test_detect_format_list():
    """Numbered/bulleted lists are detected."""
    coord = _make_coordinator()
    fmt = coord._detect_output_format("1. First\n2. Second\n3. Third")
    assert "列表" in fmt


def test_detect_format_plain():
    """Plain text returns empty string."""
    coord = _make_coordinator()
    assert coord._detect_output_format("Just a simple answer.") == ""


# --- _parse_error ---

def test_parse_error_string():
    """String errors are returned as-is for both type and detail."""
    etype, edetail = Coordinator._parse_error("timeout occurred")
    assert etype == "timeout occurred"
    assert edetail == "timeout occurred"


def test_parse_error_dict():
    """Dict errors extract 'type' and 'detail' keys."""
    etype, edetail = Coordinator._parse_error({"type": "NetworkError", "detail": "connection refused"})
    assert etype == "NetworkError"
    assert edetail == "connection refused"


def test_parse_error_dict_missing_keys():
    """Dict without keys uses defaults."""
    etype, edetail = Coordinator._parse_error({})
    assert etype == "unknown"
    assert "{}" in edetail


def test_parse_error_none():
    """None error returns 'unknown' type."""
    etype, edetail = Coordinator._parse_error(None)
    assert etype == "unknown"
    assert edetail == "unknown"


# --- _parse_planned_tools ---

def test_parse_planned_tools_ordered():
    """Tools are returned in first-occurrence order."""
    coord = _make_coordinator()
    plan = "First use web_search, then calc, then web_search again"
    result = coord._parse_planned_tools(plan, {"web_search", "calc", "system_run"})
    assert result == ["web_search", "calc"]


def test_parse_planned_tools_empty_plan():
    """Empty plan returns empty list."""
    coord = _make_coordinator()
    assert coord._parse_planned_tools("", {"web_search"}) == []


def test_parse_planned_tools_no_available():
    """No available tools returns empty list."""
    coord = _make_coordinator()
    assert coord._parse_planned_tools("use web_search now", set()) == []


def test_parse_planned_tools_unknown_names():
    """Names not in available set are skipped."""
    coord = _make_coordinator()
    result = coord._parse_planned_tools("use web_search and foobar", {"web_search"})
    assert result == ["web_search"]


# --- _should_show_suggestions ---

def test_show_suggestions_default():
    """Without ctx/config, defaults to True."""
    coord = _make_coordinator()
    assert coord._should_show_suggestions() is True


def test_show_suggestions_config_disabled():
    """Config can disable suggestions."""
    coord = _make_coordinator()
    coord.ctx = type("Ctx", (), {"config": {"agent": {"show_suggestions": False}}})()
    assert coord._should_show_suggestions() is False


# --- _extract_entities ---

def test_extract_entities_no_ctx_skips():
    """Without ctx, extraction is a no-op."""
    coord = _make_coordinator()
    turn = _make_turn(input_text="test")
    turn.result = "some answer"
    # Should not raise
    coord._extract_entities(turn)


def test_extract_entities_error_turn_skips():
    """Error turns are skipped (no entity extraction)."""
    coord = _make_coordinator()
    turn = _make_turn(input_text="test")
    turn.error = "some error"
    turn.result = "some answer"
    # Mock ctx with memory._kg
    coord.ctx = type("Ctx", (), {
        "config": {},
        "memory": type("M", (), {"_kg": None})(),
    })()
    coord._extract_entities(turn)  # should not raise


def test_extract_entities_empty_result_skips():
    """Turns with empty results are skipped."""
    coord = _make_coordinator()
    turn = _make_turn(input_text="test")
    turn.result = ""
    coord.ctx = type("Ctx", (), {
        "config": {},
        "memory": type("M", (), {"_kg": None})(),
    })()
    coord._extract_entities(turn)  # should not raise


# --- _compute_dynamic_temperature ---

def test_dynamic_temp_default():
    """Without task_types or complexity, returns base temperature."""
    from core.coordinator import DYNAMIC_TEMP_BASE
    coord = _make_coordinator()
    turn = _make_turn()
    turn.estimated_complexity = 0.1
    temp = coord._compute_dynamic_temperature(turn)
    assert temp == DYNAMIC_TEMP_BASE


def test_dynamic_temp_high_complexity():
    """High complexity returns lower temperature."""
    from core.coordinator import EXPERT_COMPLEXITY_THRESHOLD
    coord = _make_coordinator()
    turn = _make_turn()
    turn.estimated_complexity = EXPERT_COMPLEXITY_THRESHOLD + 0.1
    temp = coord._compute_dynamic_temperature(turn)
    assert temp == 0.15


def test_dynamic_temp_creative_task():
    """Creative tasks get higher temperature."""
    from core.coordinator import DYNAMIC_TEMP_CREATIVE
    coord = _make_coordinator()
    turn = _make_turn()
    turn.estimated_complexity = 0.5
    turn.meta["task_types"] = ["creative", "writing"]
    temp = coord._compute_dynamic_temperature(turn)
    assert temp == DYNAMIC_TEMP_CREATIVE


def test_dynamic_temp_factual_task():
    """Factual tasks get lower temperature."""
    from core.coordinator import DYNAMIC_TEMP_FACTUAL
    coord = _make_coordinator()
    turn = _make_turn()
    turn.estimated_complexity = 0.5
    turn.meta["task_types"] = ["factual", "coding"]
    temp = coord._compute_dynamic_temperature(turn)
    assert temp == DYNAMIC_TEMP_FACTUAL

