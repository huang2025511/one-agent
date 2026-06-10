"""Event-driven microkernel: EventBus and Event types.

Inspired by OpenSquilla's microkernel design: everything passes through a
central event bus so components can be added / removed without cross coupling.

Enhanced with:
  - Dead-letter queue for unhandled events
  - Message tracking (sent / processing / done / failed)
  - Priority-based dispatch with backlog reporting
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
    so the system remains responsive to guardrails.
    """

    LOW = 0
    NORMAL = 5
    HIGH = 8
    CRITICAL = 10


class EventStatus(enum.Enum):
    SENT = "sent"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


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
    status: EventStatus = EventStatus.SENT
    processed_at: Optional[float] = None
    error: Optional[str] = None

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __contains__(self, key: str) -> bool:
        return key in self.payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def mark_processing(self) -> None:
        self.status = EventStatus.PROCESSING

    def mark_done(self) -> None:
        self.status = EventStatus.DONE
        self.processed_at = time.time()

    def mark_failed(self, error: str) -> None:
        self.status = EventStatus.FAILED
        self.error = error
        self.processed_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "priority": self.priority.name,
            "source": self.source,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "processed_at": self.processed_at,
            "error": self.error,
            "duration_ms": (
                round((self.processed_at - self.timestamp) * 1000, 2)
                if self.processed_at
                else None
            ),
        }


Handler = Callable[[Event], Any]


class EventBus:
    """Central pub/sub bus with dead-letter queue and message tracking.

    Plugins subscribe by event type.  Handlers may be coroutines; the bus
    runs them through the shared asyncio loop.
    """

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._subscribers: Dict[str, List[Handler]] = {}
        self._wildcards: List[Handler] = []
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue_size)
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Dead-letter queue for unhandled events
        self._dead_letter_queue: List[Event] = []
        self._dlq_limit = 500

        # Message tracker: id -> Event
        self._tracker: Dict[str, Event] = {}
        self._tracker_limit = 2000

        # Metrics
        self._metrics = {
            "published": 0,
            "processed": 0,
            "dead_lettered": 0,
            "errors": 0,
            "started_at": time.time(),
        }

    # ---------------------------------------------------------------- public
    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)
        logger.debug("subscribed %s to %s", handler, event_type)

    def subscribe_all(self, handler: Handler) -> None:
        self._wildcards.append(handler)

    def publish(self, event) -> None:
        """Accept either an Event object OR a dict with 'type'/'payload' keys."""
        if isinstance(event, dict):
            evt = Event(
                type=event.get("type", "unknown"),
                payload=event.get("payload", {}),
                source=event.get("source", "bus"),
                context_id=event.get("context_id"),
            )
        else:
            evt = event

        # Track the event
        self._track(evt)
        self._metrics["published"] += 1

        if not self._running:
            logger.debug("bus not running — queueing %s", evt.type)
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            logger.warning("event queue full, dropping %s (id=%s)", evt.type, evt.id)
            self._add_to_dlq(evt)

    # ---------------------------------------------------------------- runtime
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
            # Batch: collect all pending items, sort by priority
            pending = [event]
            while not self._queue.empty():
                try:
                    pending.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            pending.sort(key=lambda e: -int(e.priority))
            for e in pending:
                await self._dispatch(e)
            await asyncio.sleep(0)

    async def _dispatch(self, event: Event) -> None:
        self._tracker[event.id] = event
        event.mark_processing()

        handlers: List[Handler] = list(self._wildcards)
        handlers.extend(self._subscribers.get(event.type, []))
        self._metrics["processed"] += 1

        if not handlers:
            logger.debug("no handlers for %s (id=%s)", event.type, event.id)
            self._add_to_dlq(event)
            event.mark_failed("no handlers registered")
            self._metrics["dead_lettered"] += 1
            return

        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("handler %s failed on %s", handler, event.type)
                event.mark_failed(str(handler))
                self._metrics["errors"] += 1

        event.mark_done()

    # ---------------------------------------------------------------- DLQ
    def _add_to_dlq(self, event: Event) -> None:
        event.status = EventStatus.DEAD_LETTER
        self._dead_letter_queue.append(event)
        if len(self._dead_letter_queue) > self._dlq_limit:
            self._dead_letter_queue.pop(0)

    # ---------------------------------------------------------------- tracker
    def _track(self, event: Event) -> None:
        self._tracker[event.id] = event
        if len(self._tracker) > self._tracker_limit:
            # Remove oldest done/failed events
            done_ids = [
                eid for eid, e in self._tracker.items()
                if e.status in (EventStatus.DONE, EventStatus.FAILED)
            ]
            for eid in done_ids[: len(self._tracker) - self._tracker_limit + len(done_ids)]:
                del self._tracker[eid]

    def get_tracked(self, event_id: str) -> Optional[Event]:
        return self._tracker.get(event_id)

    def get_dlq(self, limit: int = 50) -> List[Event]:
        return list(reversed(self._dead_letter_queue))[:limit]

    def clear_dlq(self) -> int:
        count = len(self._dead_letter_queue)
        self._dead_letter_queue.clear()
        return count

    # ---------------------------------------------------------------- metrics
    def metrics(self) -> Dict[str, Any]:
        uptime = time.time() - self._metrics["started_at"]
        return {
            **self._metrics,
            "uptime_seconds": round(uptime, 2),
            "queue_depth": self._queue.qsize(),
            "tracker_size": len(self._tracker),
            "dlq_size": len(self._dead_letter_queue),
            "events_per_second": round(self._metrics["published"] / uptime, 3)
            if uptime > 1
            else 0,
        }
