"""Smoke test — drives Athena end-to-end with a stub LLM.

This replaces LLM HTTP calls with a deterministic fake so we can verify
the pipeline (router → memory → skills → coordinator → gateways) works
without touching the network.

Run:  python tests/smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.agent import load_config  # noqa: E402
from core.context import AgentContext  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.coordinator import Coordinator  # noqa: E402
from models import LLMProvider  # noqa: E402
from router import SmartRouter, HistoryRecorder  # noqa: E402
from memory import MemoryPlugin  # noqa: E402
from skills import SkillManager  # noqa: E402
from executors import ShellExecutor  # noqa: E402
from scheduler import SchedulerPlugin  # noqa: E402


class StubLLM(LLMProvider):
    """Returns a canned response to every call, but still tracks tool call
    style so we can test routing."""

    def __init__(self) -> None:
        super().__init__()
        self._default_model = "stub"

    async def chat_completion(self, messages, model=None, temperature=None, max_tokens=None, tools=None):
        # decide: if the last user message contains a keyword we recognise,
        # produce a plausible reply; otherwise just echo the intent.
        user_text = ""
        for m in reversed(messages):
            if isinstance(m.get("content"), str) and m["content"]:
                user_text = m["content"]
                break
        if "hello" in user_text.lower() or "hi " in user_text.lower():
            text = "Hello from Athena (stub) — pipeline confirmed working."
        elif "echo" in user_text.lower():
            text = f"echo reply: {user_text}"
        elif "calc" in user_text.lower() or "2+2" in user_text:
            # pretend the calc tool was called
            text = "2 + 2 = 4 (stub tool path)"
        else:
            text = f"stub-LLM reply to: {user_text[:60]}"
        return {"text": text, "tool_calls": [], "tokens_used": len(user_text) + len(text), "model": "stub"}


async def main() -> int:
    cfg = load_config(str(ROOT / "config" / "default_config.yaml"))
    # force router.self_evolution.enabled = True etc.
    bus = EventBus()
    ctx = AgentContext(config=cfg, bus=bus)
    ctx._plugins = []  # type: ignore[attr-defined]

    llm = StubLLM()
    router = SmartRouter()
    history = HistoryRecorder()
    memory = MemoryPlugin()
    skills = SkillManager()
    shell = ShellExecutor()
    scheduler = SchedulerPlugin()
    coord = Coordinator()

    # register
    from core.plugin import PluginManager
    pm = PluginManager()
    for p in (llm, router, history, memory, skills, shell, scheduler, coord):
        pm.register(p)
    ctx._plugins = [llm, router, history, memory, skills, shell, scheduler, coord]  # type: ignore[attr-defined]

    await bus.start()
    await pm.setup_all(ctx)
    router.bind_llm(llm)
    coord.bind(llm, skills)
    await pm.start_all()

    # send a few messages
    test_cases = [
        "Hi, are you there?",
        "Please echo: Athena smoke test",
        "What is 2 + 2?",
        "Give me a short status report of the agent",
    ]
    results = []
    from core.context import TurnContext
    for text in test_cases:
        turn = TurnContext(input_text=text, source="smoke", session_id="smoke-session")
        bus.publish({"type": "user_message", "payload": {"turn": turn, "session_id": "smoke-session"}, "source": "smoke"})
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline:
            if turn.result is not None or turn.error is not None:
                break
            await asyncio.sleep(0.1)
        results.append((text, turn.result, turn.model, turn.tokens_used, turn.estimated_complexity, turn.skills))

    await pm.stop_all()
    await bus.stop()

    # print summary
    print("\n=== smoke results ===")
    for q, a, model, tok, complexity, skills in results:
        print(f"Q: {q[:60]}")
        print(f"  A: {a[:80]}")
        print(f"  model={model} tokens={tok} complexity={complexity:.2f} skills={skills}")
        print()
    # verify
    ok = all(a for _, a, *_ in results)
    print("overall:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
