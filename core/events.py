"""Event-driven microkernel: EventBus and Event types.

Inspired by OpenSquilla's microkernel design: everything passes through a
central event bus so components can be added / removed without cross coupling.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)


class EventPriority(enum.IntEnum):
    """Execution hint for the event bus.

    CRITICAL events (interrupt, emergency stop) are dispatched before others
    so the system remains responsive to guardrails.  NORMAL is the default.
    """

    LOW = 0
    NORMAL = 5
    HIGH = 8
    CRITICAL = 10


@dataclass
class Event:
    """Generic event envelope.

    Every message flowing through the agent carries metadata used by the
    scheduler, the router, and the audit log — without polluting the LLM
    payload itself.
    """

    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: EventPriority = EventPriority.NORMAL
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    source: str = "unknown"
    context_id: Optional[str] = None
    propagated: bool = False

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __contains__(self, key: str) -> bool:
        return key in self.payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


Handler = Callable[[Event], Any]


class EventBus:
    """Central pub/sub bus.

    Plugins subscribe by event type.  Handlers may be coroutines; the bus
    runs them through the shared asyncio loop.  We deliberately keep this
    class tiny — the "real" logic lives in subscribers.
    """

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._subscribers: Dict[str, List[Handler]] = {}
        self._wildcards: List[Handler] = []
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue_size)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._event_history: List[Event] = []
        self._history_limit = 200

    # ---------------------------------------------------------------- public
    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)
        logger.debug("subscribed %s to %s", handler, event_type)

    def subscribe_all(self, handler: Handler) -> None:
        self._wildcards.append(handler)

    def publish(self, event) -> None:
        # accept either an Event object OR a dict with "type"/"payload" keys
        if isinstance(event, dict):
            evt = Event(
                type=event.get("type", "unknown"),
                payload=event.get("payload", {}),
                source=event.get("source", "bus"),
                context_id=event.get("context_id"),
            )
        else:
            evt = event
        if not self._running:
            logger.debug("bus not running — queueing %s", evt.type)
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            logger.warning("event queue full, dropping %s", evt.type)

    # --------------------------------------------------------------- runtime
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("event bus started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("event bus stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                break
            # high-priority first — crude scan, acceptable with small queues
            pending = [event]
            while not self._queue.empty():
                pending.append(self._queue.get_nowait())
            pending.sort(key=lambda e: -int(e.priority))
            for e in pending:
                await self._dispatch(e)
            await asyncio.sleep(0)  # yield

    async def _dispatch(self, event: Event) -> None:
        self._event_history.append(event)
        if len(self._event_history) > self._history_limit:
            self._event_history.pop(0)
        handlers: List[Handler] = list(self._wildcards)
        handlers.extend(self._subscribers.get(event.type, []))
        if not handlers:
            return
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("handler %s failed on %s", handler, event.type)
