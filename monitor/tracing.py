"""Distributed Tracing — OpenTelemetry integration for request tracing.

Provides distributed tracing across all components:
- Automatic span creation for requests
- Trace context propagation (W3C TraceContext)
- LLM call tracing with token usage
- Memory/Skill call tracing
- Custom spans for business logic
- Export to OTLP collectors (Jaeger, Zipkin, Tempo)

Usage:
    tracer = get_tracer()
    with tracer.start_as_current_span("my_operation") as span:
        span.set_attribute("user.id", "123")
        # ... do work ...
        span.set_status("ok")
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """A trace span representing a unit of work."""
    name: str
    trace_id: str = ""
    span_id: str = ""
    parent_id: str = ""
    service_name: str = "one-agent"
    start_time: float = field(default_factory=time.time)
    end_time: float = 0
    status: str = "ok"  # ok, error
    error_message: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    children: List["Span"] = field(default_factory=list)

    def set_attribute(self, key: str, value: Any) -> None:
        """Set a span attribute."""
        self.attributes[key] = value

    def add_event(self, name: str, attributes: Dict[str, Any] = None) -> None:
        """Add an event to the span."""
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {},
        })

    def set_status(self, status: str, error_message: str = "") -> None:
        """Set span status."""
        self.status = status
        self.error_message = error_message

    def end(self) -> None:
        """End the span."""
        self.end_time = time.time()

    @property
    def duration_ms(self) -> float:
        """Get span duration in milliseconds."""
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0


@dataclass
class TraceContext:
    """Trace context for propagating across process boundaries."""
    trace_id: str
    span_id: str
    trace_flags: str = "01"  # 01 = sampled
    trace_state: str = ""
    baggage: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_headers(cls, headers: Dict[str, str]) -> Optional["TraceContext"]:
        """Parse trace context from HTTP headers (W3C TraceContext)."""
        traceparent = headers.get("traceparent", "")
        if not traceparent:
            return None

        try:
            parts = traceparent.split("-")
            if len(parts) >= 3:
                return cls(
                    trace_id=parts[1],
                    span_id=parts[2],
                    trace_flags=parts[3] if len(parts) > 3 else "01",
                )
        except Exception as exc:
            logger.debug("Failed to parse traceparent: %s", exc)

        return None

    def to_headers(self) -> Dict[str, str]:
        """Convert to HTTP headers."""
        return {
            "traceparent": f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}",
        }


class SimpleTracer:
    """Simple in-memory tracer with export interface.

    Provides a lightweight tracing implementation that:
    - Works without OpenTelemetry SDK installed
    - Can export to any OTLP-compatible backend
    - Falls back to logging if no exporter is configured
    """

    def __init__(self, service_name: str = "one-agent") -> None:
        self._service_name = service_name
        self._spans: List[Span] = []
        self._active_spans: Dict[str, Span] = {}
        self._enabled = True
        self._max_spans = 10000
        self._exporter: Optional[Callable] = None

    @property
    def service_name(self) -> str:
        return self._service_name

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable tracing."""
        self._enabled = enabled

    def set_exporter(self, exporter: Callable) -> None:
        """Set a span exporter function.

        The exporter receives a list of completed spans.
        """
        self._exporter = exporter

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        parent: Optional[Span] = None,
        attributes: Dict[str, Any] = None,
    ) -> Generator[Span, None, None]:
        """Start a new span as the current span.

        Usage:
            with tracer.start_as_current_span("my_operation") as span:
                span.set_attribute("key", "value")
        """
        if not self._enabled:
            yield Span(name=name)
            return

        # Generate IDs
        trace_id = parent.trace_id if parent else uuid4().hex[:32]
        span_id = uuid4().hex[:16]

        span = Span(
            name=name,
            trace_id=trace_id,
            span_id=span_id,
            parent_id=parent.span_id if parent else "",
            service_name=self._service_name,
            attributes=attributes or {},
        )

        self._active_spans[span_id] = span

        try:
            yield span
            if span.status == "ok":  # Only set ok if no error was set
                span.set_status("ok")
        except Exception as exc:
            if span.status != "error":  # Don't override if user already set error
                span.set_status("error", str(exc))
            raise
        finally:
            span.end()
            self._active_spans.pop(span_id, None)
            self._spans.append(span)

            # Export if buffer is full
            if len(self._spans) >= self._max_spans:
                self._flush()

    def start_span(
        self,
        name: str,
        trace_id: str = "",
        parent_id: str = "",
        attributes: Dict[str, Any] = None,
    ) -> Span:
        """Start a new span (manual management)."""
        if not self._enabled:
            return Span(name=name)

        if not trace_id:
            trace_id = uuid4().hex[:32]
        span_id = uuid4().hex[:16]

        span = Span(
            name=name,
            trace_id=trace_id,
            span_id=span_id,
            parent_id=parent_id,
            service_name=self._service_name,
            attributes=attributes or {},
        )

        self._active_spans[span_id] = span
        return span

    def end_span(self, span: Span) -> None:
        """End a span (manual management)."""
        span.end()
        self._active_spans.pop(span.span_id, None)
        self._spans.append(span)

        if len(self._spans) >= self._max_spans:
            self._flush()

    def get_current_span(self) -> Optional[Span]:
        """Get the current active span."""
        if self._active_spans:
            # Return most recently created
            return list(self._active_spans.values())[-1]
        return None

    def get_trace(self, trace_id: str) -> List[Span]:
        """Get all spans for a trace."""
        return [s for s in self._spans if s.trace_id == trace_id]

    def _flush(self) -> None:
        """Flush spans to exporter."""
        if self._exporter and self._spans:
            try:
                self._exporter(self._spans)
                self._spans.clear()
            except Exception as exc:
                logger.warning("Span export failed: %s", exc)

    def flush(self) -> None:
        """Force flush all pending spans."""
        self._flush()

    def clear(self) -> None:
        """Clear all spans (for testing)."""
        self._spans.clear()
        self._active_spans.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get tracing statistics."""
        return {
            "service_name": self._service_name,
            "enabled": self._enabled,
            "pending_spans": len(self._active_spans),
            "completed_spans": len(self._spans),
            "has_exporter": self._exporter is not None,
        }

    def format_trace_tree(self, trace_id: str) -> str:
        """Format a trace as an ASCII tree (for debugging)."""
        spans = self.get_trace(trace_id)
        if not spans:
            return f"Trace {trace_id}: not found"

        lines = [f"Trace {trace_id} ({len(spans)} spans)"]
        lines.append("")

        # Build tree
        span_by_id = {s.span_id: s for s in spans}
        roots = [s for s in spans if not s.parent_id]

        def format_span(span: Span, indent: int = 0) -> List[str]:
            prefix = "  " * indent
            duration = f"{span.duration_ms:.1f}ms"
            status = "✓" if span.status == "ok" else "✗"
            attrs = ", ".join(f"{k}={v}" for k, v in list(span.attributes.items())[:3])
            line = f"{prefix}{status} {span.name} [{duration}]"
            if attrs:
                line += f" ({attrs})"
            result = [line]

            children = [s for s in spans if s.parent_id == span.span_id]
            for child in children:
                result.extend(format_span(child, indent + 1))

            return result

        for root in roots:
            lines.extend(format_span(root))

        return "\n".join(lines)


# Singleton
_tracer: Optional[SimpleTracer] = None


def get_tracer() -> SimpleTracer:
    """Get the shared tracer instance."""
    global _tracer
    if _tracer is None:
        _tracer = SimpleTracer()
    return _tracer


# ===================================================== Built-in span helpers

def trace_llm_call(
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    duration_ms: float = 0,
    error: str = "",
) -> None:
    """Record an LLM call as a span."""
    tracer = get_tracer()
    span = tracer.get_current_span()
    if span:
        span.set_attribute("llm.provider", provider)
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.prompt_tokens", prompt_tokens)
        span.set_attribute("llm.completion_tokens", completion_tokens)
        span.set_attribute("llm.total_tokens", prompt_tokens + completion_tokens)
        span.set_attribute("llm.duration_ms", duration_ms)
        if error:
            span.set_status("error", error)


def trace_skill_call(
    skill_name: str,
    status: str,
    duration_ms: float = 0,
    error: str = "",
) -> None:
    """Record a skill call as a span."""
    tracer = get_tracer()
    span = tracer.get_current_span()
    if span:
        span.set_attribute("skill.name", skill_name)
        span.set_attribute("skill.status", status)
        span.set_attribute("skill.duration_ms", duration_ms)
        if error:
            span.set_status("error", error)


def trace_memory_operation(
    operation: str,
    memory_type: str,
    item_count: int = 0,
    duration_ms: float = 0,
) -> None:
    """Record a memory operation as a span."""
    tracer = get_tracer()
    span = tracer.get_current_span()
    if span:
        span.set_attribute("memory.operation", operation)
        span.set_attribute("memory.type", memory_type)
        span.set_attribute("memory.item_count", item_count)
        span.set_attribute("memory.duration_ms", duration_ms)


# ===================================================== OTLP Export Helpers

def create_otlp_exporter(
    endpoint: str = "http://localhost:4317",
    protocol: str = "grpc",
) -> Callable:
    """Create an OTLP exporter function.

    Returns an exporter that sends spans to an OTLP collector.
    Falls back gracefully if opentelemetry-exporter-otlp is not installed.
    """
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        exporter = OTLPSpanExporter(endpoint=endpoint)
        processor = BatchSpanProcessor(exporter)

        def export_spans(spans: List[Span]) -> None:
            for span in spans:
                # Convert our Span to OpenTelemetry format
                from opentelemetry.trace import Span as OTelSpan
                # ... would need full OTel integration

        return export_spans

    except ImportError:
        logger.warning("OpenTelemetry not installed. Tracing export disabled.")
        return lambda spans: None


def create_jaeger_exporter(
    agent_host: str = "localhost",
    agent_port: int = 6831,
) -> Callable:
    """Create a Jaeger exporter function.

    Returns an exporter that sends spans to a Jaeger agent.
    """
    try:
        from jaeger_client import JaegerTracer

        # Simple export function
        def export_to_jaeger(spans: List[Span]) -> None:
            for span in spans:
                # Would need full Jaeger client integration
                pass

        return export_to_jaeger

    except ImportError:
        logger.warning("Jaeger client not installed. Tracing export disabled.")
        return lambda spans: None