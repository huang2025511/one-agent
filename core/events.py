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
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)

# Event bus configuration constants
MAX_QUEUE_SIZE = 1000
MAX_PAYLOAD_SIZE = 1_000_000  # 1MB
DEAD_LETTER_QUEUE_LIMIT = 500
TRACKER_LIMIT = 2000
TRACKER_TTL = 3600  # 1 hour
TRACKER_CLEANUP_INTERVAL = 60


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
    PARTIAL = "partial"  # Some handlers succeeded, some failed
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

    def __post_init__(self):
        if self.priority is None:
            self.priority = EventPriority.NORMAL

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

    # Allowed event types - prevents injection of arbitrary event types
    _ALLOWED_EVENT_TYPES = {
        # Core events
        "turn_start", "turn_complete", "turn_completed", "turn_failed", "turn_routed",
        "skill_executed", "skill_failed",
        "memory_added", "memory_searched",
        "config_changed",
        "session_created", "session_updated",
        "approval_requested", "approval_resolved", "approval_needed",
        "alert_triggered",
        "mcp_tool_called",
        "python_executed",
        "cron_triggered",
        "shutdown",
        "startup",
        # User interaction
        "user_message",
    }

    def __init__(self, max_queue_size: int = MAX_QUEUE_SIZE) -> None:
        self._subscribers: Dict[str, List[Handler]] = {}
        self._wildcards: List[Handler] = []
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue_size)
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Dead-letter queue for unhandled events (bounded deque to prevent memory leak)
        self._dead_letter_queue: deque[Event] = deque(maxlen=DEAD_LETTER_QUEUE_LIMIT)
        self._dlq_limit = DEAD_LETTER_QUEUE_LIMIT

        # Message tracker: id -> Event, with TTL-based expiration
        self._tracker: Dict[str, Event] = {}
        self._tracker_timestamps: Dict[str, float] = {}
        self._tracker_limit = TRACKER_LIMIT
        self._tracker_ttl = TRACKER_TTL
        self._tracker_cleanup_interval = TRACKER_CLEANUP_INTERVAL
        self._tracker_ops_since_cleanup = 0

        # Metrics
        self._metrics = {
            "published": 0,
            "processed": 0,
            "dead_lettered": 0,
            "errors": 0,
            "started_at": time.time(),
        }
        # Per-event-type counters (v2.1) — lets the monitor dashboard
        # see "which events fired and how often".  Cheap dict, no TTL.
        self._by_type: Dict[str, int] = {}

    # ---------------------------------------------------------------- public
    def subscribe(self, event_type: str, handler: Handler) -> None:
        assert event_type, "event_type cannot be empty"
        assert isinstance(event_type, str), "event_type must be a string"
        assert handler is not None, "handler cannot be None"
        assert callable(handler), "handler must be callable"
        
        self._subscribers.setdefault(event_type, []).append(handler)
        logger.info("subscribed %s to %s", handler, event_type)

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
                priority=event.get("priority"),
            )
        else:
            evt = event

        # Validate event type to prevent injection attacks
        if evt.type not in self._ALLOWED_EVENT_TYPES:
            logger.warning(
                "rejected unknown event type '%s' from source '%s'",
                evt.type, evt.source
            )
            self._metrics["errors"] += 1
            return

        # Validate payload size to prevent DoS
        payload_size = len(str(evt.payload))
        if payload_size > MAX_PAYLOAD_SIZE:
            logger.warning(
                "rejected oversized payload (%d bytes) for event type '%s'",
                payload_size, evt.type
            )
            self._metrics["errors"] += 1
            return

        # Track the event
        self._track(evt)
        self._metrics["published"] += 1
        # Per-type counter (cheap; safe for the hot publish path)
        self._by_type[evt.type] = self._by_type.get(evt.type, 0) + 1

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
            for i, e in enumerate(pending):
                await self._dispatch(e)
                # periodically yield to prevent starving other coroutines
                if (i + 1) % 50 == 0:
                    await asyncio.sleep(0)
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

        handler_errors: List[str] = []
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except (ValueError, KeyError, TypeError, RuntimeError, asyncio.TimeoutError) as exc:
                logger.error("handler %s failed on %s: %s", handler, event.type, exc, exc_info=True)
                handler_errors.append(str(handler))
                self._metrics["errors"] += 1
            except Exception as exc:
                logger.error("handler %s failed on %s with unexpected error: %s", handler, event.type, exc, exc_info=True)
                handler_errors.append(str(handler))
                self._metrics["errors"] += 1

        # Mark event status based on handler results
        if not handler_errors:
            # All handlers succeeded
            event.mark_done()
        elif len(handler_errors) == len(handlers):
            # All handlers failed
            event.mark_failed("; ".join(handler_errors))
        else:
            # Partial success — some handlers succeeded, some failed
            event.status = EventStatus.PARTIAL
            event.error = f"{len(handler_errors)}/{len(handlers)} handlers failed"
            logger.warning(
                "event %s partial success: %d/%d handlers failed",
                event.type, len(handler_errors), len(handlers),
            )

    # ---------------------------------------------------------------- DLQ
    def _add_to_dlq(self, event: Event) -> None:
        event.status = EventStatus.DEAD_LETTER
        # deque automatically evicts oldest when maxlen is reached
        self._dead_letter_queue.append(event)

    # ---------------------------------------------------------------- tracker
    def _track(self, event: Event) -> None:
        self._tracker[event.id] = event
        self._tracker_timestamps[event.id] = time.time()

        # Periodically clean up expired tracker entries
        self._tracker_ops_since_cleanup += 1
        if self._tracker_ops_since_cleanup >= self._tracker_cleanup_interval:
            self._cleanup_tracker()
            self._tracker_ops_since_cleanup = 0

        # If still over limit after cleanup, evict oldest entries
        if len(self._tracker) > self._tracker_limit:
            # Remove oldest done/failed events in insertion order
            done_ids = [
                eid for eid, e in self._tracker.items()
                if e.status in (EventStatus.DONE, EventStatus.FAILED, EventStatus.DEAD_LETTER)
            ]
            # tracker is a regular dict (insertion-ordered in py3.7+)
            # remove from the front while keeping only the last _tracker_limit
            excess = len(self._tracker) - self._tracker_limit
            # Prefer evicting done/failed before evicting still-processing
            victims = done_ids[:excess] if len(done_ids) >= excess else done_ids
            for eid in victims:
                del self._tracker[eid]
                self._tracker_timestamps.pop(eid, None)
            # If we still overflow (e.g. all events are in-flight), drop from front
            while len(self._tracker) > self._tracker_limit:
                oldest_id = next(iter(self._tracker))
                del self._tracker[oldest_id]
                self._tracker_timestamps.pop(oldest_id, None)

    def _cleanup_tracker(self) -> None:
        """Remove expired tracker entries based on TTL."""
        now = time.time()
        expired = [
            eid for eid, ts in self._tracker_timestamps.items()
            if now - ts > self._tracker_ttl
        ]
        for eid in expired:
            del self._tracker[eid]
            del self._tracker_timestamps[eid]
        if expired:
            logger.debug("cleaned up %d expired tracker entries", len(expired))

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
        # Top-10 event types so the monitor JSON stays small
        top_types = sorted(
            self._by_type.items(), key=lambda kv: kv[1], reverse=True,
        )[:10]
        return {
            **self._metrics,
            "uptime_seconds": round(uptime, 2),
            "queue_depth": self._queue.qsize(),
            "tracker_size": len(self._tracker),
            "dlq_size": len(self._dead_letter_queue),
            "events_per_second": round(self._metrics["published"] / uptime, 3)
            if uptime > 1
            else 0,
            "by_type": dict(top_types),
        }
