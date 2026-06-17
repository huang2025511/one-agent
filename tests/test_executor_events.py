"""Unit tests for the executor interface and EventBus payload handling.

Part 1 — Executor interface (executors/base.py + executors/__init__.py):
  - ExecutorResult canonical properties (success/stdout/stderr/exit_code/error/blocked)
  - ExecutorResult legacy aliases (returncode/ok/output)
  - _to_executor_result() legacy-dict normalization
  - BaseExecutor subclasses all implement execute()
  - ShellExecutor.execute() delegates to run()
  - BrowserExecutor.execute() delegates to fetch()
  - All executors inherit BaseExecutor

Part 2 — EventBus payload handling (core/events.py):
  - publish() nested dict  -> payload holds the nested dict
  - publish() flat dict    -> non-reserved keys merged into payload
  - publish() mixed dict   -> nested payload + extra keys merged
  - publish() Event object -> payload passed through unchanged
  - reserved keys (type/payload/source/context_id/priority) not merged into payload

Note: EventBus rejects event types not in its allow-list, so the payload
tests use allowed types (``turn_start`` / ``external_message``) instead of
the literal ``"test"`` so the events are actually dispatched and observable.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# Part 1: Executor interface tests
# ============================================================

def test_executor_result_canonical_properties():
    """ExecutorResult exposes success/stdout/stderr/exit_code/error/blocked."""
    from executors.base import ExecutorResult

    r = ExecutorResult({
        "success": True,
        "stdout": "hello",
        "stderr": "warn",
        "exit_code": 0,
        "error": None,
        "blocked": False,
    })
    assert r.success is True
    assert r.stdout == "hello"
    assert r.stderr == "warn"
    assert r.exit_code == 0
    assert r.error is None
    assert r.blocked is False


def test_executor_result_defaults_when_fields_missing():
    """ExecutorResult properties return sensible defaults for missing keys."""
    from executors.base import ExecutorResult

    r = ExecutorResult({})
    # exit_code defaults to 0; success derives from exit_code==0 and not blocked
    assert r.exit_code == 0
    assert r.success is True
    assert r.stdout == ""
    assert r.stderr == ""
    assert r.error is None
    assert r.blocked is False


def test_executor_result_legacy_aliases():
    """Legacy aliases returncode/ok/output mirror canonical fields."""
    from executors.base import ExecutorResult

    r = ExecutorResult({"success": True, "stdout": "out", "exit_code": 0})
    assert r.returncode == r.exit_code == 0
    assert r.ok is r.success is True
    assert r.output == r.stdout == "out"


def test_executor_result_legacy_alias_reflects_failure():
    """returncode/ok/output aliases reflect a failing result."""
    from executors.base import ExecutorResult

    r = ExecutorResult({"exit_code": 2, "stdout": "partial", "stderr": "boom"})
    assert r.returncode == 2
    assert r.ok is False
    assert r.success is False
    assert r.output == "partial"


def test_to_executor_result_maps_returncode_to_exit_code():
    """_to_executor_result maps legacy 'returncode' to canonical 'exit_code'."""
    from executors.base import ExecutorResult, _to_executor_result

    result = _to_executor_result({"returncode": 0, "stdout": "ok"})
    assert isinstance(result, ExecutorResult)
    assert result.exit_code == 0
    assert result.returncode == 0
    assert result.success is True
    assert result.stdout == "ok"
    assert result["metadata"] == {}


def test_to_executor_result_maps_ok_and_output():
    """_to_executor_result derives exit_code from 'ok' and stdout from 'output'."""
    from executors.base import _to_executor_result

    ok = _to_executor_result({"ok": True, "output": "done"})
    assert ok.exit_code == 0
    assert ok.success is True
    assert ok.stdout == "done"  # 'output' mapped to 'stdout'

    fail = _to_executor_result({"ok": False, "stderr": "nope"})
    assert fail.exit_code == 1
    assert fail.success is False
    assert fail.error == "nope"  # stderr becomes error when unsuccessful


def test_to_executor_result_blocked_is_not_success():
    """_to_executor_result marks blocked results as unsuccessful."""
    from executors.base import _to_executor_result

    result = _to_executor_result({"returncode": 0, "blocked": True, "stderr": "blocked"})
    assert result.blocked is True
    assert result.success is False


def test_to_executor_result_preserves_explicit_success():
    """Explicit 'success' in the legacy dict is preserved over derivation."""
    from executors.base import _to_executor_result

    result = _to_executor_result({"success": True, "returncode": 1})
    assert result.success is True  # explicit value wins
    assert result.exit_code == 1   # returncode still mapped


def test_all_executor_subclasses_implement_execute():
    """Every BaseExecutor subclass overrides execute() in its own __dict__."""
    from executors import BrowserExecutor, DockerExecutor, ShellExecutor
    from executors.python_runner import PythonExecutor
    from executors.system import SystemExecutor

    for cls in (ShellExecutor, DockerExecutor, BrowserExecutor,
                PythonExecutor, SystemExecutor):
        assert "execute" in cls.__dict__, f"{cls.__name__} must define execute()"


def test_all_executors_inherit_base_executor():
    """All executors are subclasses of BaseExecutor."""
    from executors import BrowserExecutor, DockerExecutor, ShellExecutor
    from executors.base import BaseExecutor
    from executors.python_runner import PythonExecutor
    from executors.system import SystemExecutor

    for cls in (ShellExecutor, DockerExecutor, BrowserExecutor,
                PythonExecutor, SystemExecutor):
        assert issubclass(cls, BaseExecutor), f"{cls.__name__} must inherit BaseExecutor"


def test_base_executor_execute_raises_not_implemented():
    """BaseExecutor.execute() is abstract — raises NotImplementedError."""
    from executors.base import BaseExecutor

    async def run():
        ex = BaseExecutor()
        with pytest.raises(NotImplementedError):
            await ex.execute()

    asyncio.run(run())


async def test_shell_executor_execute_delegates_to_run():
    """ShellExecutor.execute() delegates to run() with command/timeout/audit."""
    from executors import ShellExecutor
    from executors.base import ExecutorResult

    ex = ShellExecutor()
    sentinel = ExecutorResult({"success": True, "stdout": "ok", "exit_code": 0})
    ex.run = AsyncMock(return_value=sentinel)

    result = await ex.execute("echo 'hello'", timeout=10)

    ex.run.assert_awaited_once()
    call = ex.run.await_args
    assert call.args[0] == "echo 'hello'"
    assert call.kwargs["timeout"] == 10
    assert call.kwargs["audit"] is True  # defaults to True
    assert result is sentinel


async def test_browser_executor_execute_delegates_to_fetch():
    """BrowserExecutor.execute() delegates to fetch() with url/max_chars."""
    from executors import BrowserExecutor
    from executors.base import ExecutorResult

    ex = BrowserExecutor()
    sentinel = ExecutorResult({"success": True, "stdout": "<html>", "exit_code": 0})
    ex.fetch = AsyncMock(return_value=sentinel)

    result = await ex.execute("https://example.com", max_chars=500)

    ex.fetch.assert_awaited_once_with("https://example.com", max_chars=500)
    assert result is sentinel


async def test_browser_executor_execute_default_max_chars():
    """BrowserExecutor.execute() defaults max_chars to 8000."""
    from executors import BrowserExecutor
    from executors.base import ExecutorResult

    ex = BrowserExecutor()
    sentinel = ExecutorResult({"success": True, "exit_code": 0})
    ex.fetch = AsyncMock(return_value=sentinel)

    await ex.execute("https://example.com")

    ex.fetch.assert_awaited_once_with("https://example.com", max_chars=8000)


# ============================================================
# Part 2: EventBus payload tests
# ============================================================

async def test_publish_nested_dict_payload():
    """publish() with nested dict: 'payload' key holds the business data."""
    from core.events import EventBus

    bus = EventBus()
    captured = []
    bus.subscribe("turn_start", captured.append)
    await bus.start()
    try:
        bus.publish({"type": "turn_start", "payload": {"key": "value"}})
        await asyncio.sleep(0.2)
        assert len(captured) == 1
        assert captured[0].payload == {"key": "value"}
    finally:
        await bus.stop()


async def test_publish_flat_dict_merges_into_payload():
    """publish() with flat dict: non-reserved keys become the payload."""
    from core.events import EventBus

    bus = EventBus()
    captured = []
    bus.subscribe("external_message", captured.append)
    await bus.start()
    try:
        bus.publish({"type": "external_message", "text": "hello", "chat_id": 123})
        await asyncio.sleep(0.2)
        assert len(captured) == 1
        evt = captured[0]
        assert evt.payload["text"] == "hello"
        assert evt.payload["chat_id"] == 123
    finally:
        await bus.stop()


async def test_publish_mixed_dict_merges_payload_and_extras():
    """publish() with mixed dict: nested payload + extra keys are merged."""
    from core.events import EventBus

    bus = EventBus()
    captured = []
    bus.subscribe("turn_start", captured.append)
    await bus.start()
    try:
        bus.publish({"type": "turn_start", "payload": {"a": 1}, "extra": "b"})
        await asyncio.sleep(0.2)
        assert len(captured) == 1
        evt = captured[0]
        assert evt.payload["a"] == 1
        assert evt.payload["extra"] == "b"
    finally:
        await bus.stop()


async def test_publish_event_object_unchanged():
    """publish() with an Event object: payload is passed through unchanged."""
    from core.events import Event, EventBus, EventPriority

    bus = EventBus()
    captured = []
    bus.subscribe("turn_start", captured.append)
    await bus.start()
    try:
        original = Event(
            type="turn_start",
            payload={"k": "v"},
            source="unit_test",
            priority=EventPriority.HIGH,
        )
        bus.publish(original)
        await asyncio.sleep(0.2)
        assert len(captured) == 1
        assert captured[0] is original
        assert captured[0].payload == {"k": "v"}
    finally:
        await bus.stop()


async def test_publish_reserved_keys_not_merged_into_payload():
    """Reserved keys (type/payload/source/context_id/priority) stay out of payload."""
    from core.events import EventBus

    bus = EventBus()
    captured = []
    bus.subscribe("turn_start", captured.append)
    await bus.start()
    try:
        bus.publish({
            "type": "turn_start",
            "payload": {"key": "value"},
            "source": "unit_test",
            "context_id": "ctx-1",
            "priority": 8,
        })
        await asyncio.sleep(0.2)
        assert len(captured) == 1
        evt = captured[0]
        # payload should only contain the nested payload data
        assert evt.payload == {"key": "value"}
        # reserved keys must NOT leak into payload
        for reserved in ("type", "payload", "source", "context_id", "priority"):
            assert reserved not in evt.payload
        # ...but they should be on the Event envelope itself
        assert evt.type == "turn_start"
        assert evt.source == "unit_test"
        assert evt.context_id == "ctx-1"
    finally:
        await bus.stop()
