"""Context containers.

AgentContext holds long-lived singletons (bus, config, providers).
TurnContext is recreated per user message and carries the short-lived state:
messages, memories, tools to invoke, token budget.  Keeping the two scopes
separate prevents a single turn from polluting global state.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .events import EventBus

current_turn_var: contextvars.ContextVar[Optional["TurnContext"]] = contextvars.ContextVar(
    "current_turn", default=None
)


@dataclass
class TurnContext:
    """Per-turn state.

    Attributes
    ----------
    input_text:
        Raw user prompt.
    model:
        The model picked by the router for this turn.
    estimated_complexity:
        0..1 score produced by the router.
    messages:
        Conversation fragment handed to the LLM (may have been compressed).
    skills:
        Skill ids loaded for this turn (lazy-loaded, so bounded).
    token_budget:
        Tokens the router is willing to spend for this turn.
    result:
        Final answer produced.
    meta:
        Arbitrary key/value data for plugins to stash per-turn state.
    """

    input_text: str
    source: str = "cli"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.monotonic)
    wall_clock_at: float = field(default_factory=time.time)  # For logging/timestamps

    model: Optional[str] = None
    estimated_complexity: float = 0.0
    messages: List[Dict[str, Any]] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    token_budget: int = 2048
    tokens_used: int = 0
    result: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: Optional[float] = None

    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Validate meta field type."""
        if not isinstance(self.meta, dict):
            raise ValueError('meta must be a dict')
        # asyncio.Event lazily binds to the running loop on wait(), so it is
        # safe to construct here even outside an event loop.
        self._done: asyncio.Event = asyncio.Event()

    # ----------------------------------------------------------- convenience
    def record_success(self, answer: str, tokens_used: int) -> None:
        self.result = answer
        self.error = None  # Clear any stale error from a concurrent timeout
        self.tokens_used = tokens_used
        self.duration_seconds = time.monotonic() - self.created_at
        self._done.set()

    def record_failure(self, error: str) -> None:
        self.error = error
        self.result = None  # Clear any stale result from a concurrent success
        self.duration_seconds = time.monotonic() - self.created_at
        self._done.set()

    async def wait_done(self) -> None:
        """Block until the turn has a result or error.

        Signaled by ``record_success`` / ``record_failure``, and by external
        callers via ``mark_done`` (e.g. a ``turn_completed`` bus subscriber
        that bridges the cases where ``turn.result`` is assigned directly).
        """
        await self._done.wait()

    def mark_done(self) -> None:
        """Signal that this turn is complete (idempotent)."""
        self._done.set()


@dataclass
class AgentContext:
    """Long-lived agent-wide context."""

    config: Dict[str, Any]
    bus: EventBus
    data_dir: str = "./data"
    started_at: float = field(default_factory=time.time)

    # counters used for self-evolution statistics
    counters: Dict[str, int] = field(default_factory=dict)

    # plugin registry populated from top-level assembly
    _plugins: List[Any] = field(default_factory=list)

    # session store for persistence (set by OneAgentApp.start)
    session_store: Any = None

    # approval manager for human-in-the-loop (set by OneAgentApp.start)
    approval_manager: Any = None

    # MCP client for external tool servers (set by OneAgentApp.start)
    mcp_client: Any = None

    # Python executor for code execution (set by OneAgentApp.start)
    python_executor: Any = None

    # 重启时间戳 — 非 0 表示刚通过 /重启 命令重启过（set by OneAgentApp.start）
    recent_restart: float = 0

    def get_plugin(self, name: str):
        """Return the first plugin registered with the given name, or None."""
        for p in self._plugins:
            if getattr(p, "name", "") == name:
                return p
        return None

    def uptime(self) -> float:
        return time.time() - self.started_at
