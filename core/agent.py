"""Top-level Athena agent.

The agent is a thin orchestrator.  It delegates the actual work to plugins
registered with the plugin manager — itself included — via the event bus.

This design mirrors OpenSquilla's "small core, pluggable harness": you can
strip every feature (memory, skills, gateways) and still have a working
minimal chat loop.  Conversely, you can stack new capabilities without
touching core code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Dict, Optional

import yaml

from .context import AgentContext, TurnContext
from .events import Event, EventBus
from .plugin import Plugin, PluginManager

logger = logging.getLogger(__name__)


def _expand_env(obj):
    """Recursively expand ${VAR} references in loaded YAML."""
    if isinstance(obj, str):
        def repl(m):
            val = os.environ.get(m.group(1), "")
            return val if val else m.group(0)
        return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", repl, obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def load_config(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _expand_env(raw)


class AthenaAgent(Plugin):
    """Root plugin — owns the main request pipeline.

    Pipeline (each step fires an event so plugins can hook in):
        1. user_message_received   → gateways feed messages here
        2. turn_routed            → router picks model + budget + skills
        3. turn_completed         → after LLM call + tool invocations
        4. turn_failed            → error reporting
    """

    name = "athena"

    def __init__(self) -> None:
        super().__init__()
        self._sessions: Dict[str, list] = {}

    # ---------------------------------------------------------------- setup
    async def setup(self, ctx: AgentContext) -> None:
        await super().setup(ctx)
        self.bus.subscribe("user_message", self._on_user_message)

    # ----------------------------------------------------------- public API
    async def chat(
        self,
        text: str,
        source: str = "cli",
        session_id: Optional[str] = None,
    ) -> str:
        """Programmatic entry point — also used by the CLI gateway."""
        if self.ctx is None or self.bus is None:
            raise RuntimeError("agent not set up")
        turn = TurnContext(input_text=text, source=source)
        if session_id is not None:
            turn.session_id = session_id
        # let the pipeline run through event handlers
        self.bus.publish(
            Event(
                type="user_message",
                payload={"turn": turn, "session_id": turn.session_id},
                source=source,
                context_id=turn.turn_id,
            )
        )
        # Synchronously wait for a "turn_completed" event that carries our
        # turn_id.  We avoid making the bus itself RPC-like: instead we rely
        # on plugins eventually publishing turn_completed.  If nothing picks
        # up the message we return a graceful fallback.
        return await self._wait_for_reply(turn)

    # ---------------------------------------------------------------- event
    async def _on_user_message(self, event: Event) -> None:
        logger.info("[%s] user message: %.60s", event.get("source"), event.get("turn").input_text if event.get("turn") else "")

    # --------------------------------------------------------------- private
    async def _wait_for_reply(self, turn: TurnContext, timeout: float = 120.0) -> str:
        """Poll-turn: other plugins eventually write into ``turn.result``.

        We use a lightweight future-based pattern: plugins call
        ``turn.record_success`` or ``turn.record_failure``; we simply await
        either condition.  No threading — everything happens on the shared
        asyncio loop.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if turn.result is not None:
                return turn.result
            if turn.error is not None:
                return f"[error] {turn.error}"
            await asyncio.sleep(0.1)
        return f"[timeout] no reply produced for {turn.turn_id}"
