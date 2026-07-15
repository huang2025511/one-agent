"""Prometheus Metrics Exporter — standardized metrics in Prometheus format.

Exposes metrics in Prometheus text format for scraping:
- Request/response counts and latencies
- Token usage by model and provider
- Cache hit/miss rates
- Memory usage
- Skill usage
- Error rates
- Custom business metrics

Usage:
    metrics = get_metrics_registry()
    metrics.counter("requests_total", labels={"method": "POST"}).inc()
    metrics.histogram("request_duration_seconds", labels={"path": "/api"}).observe(0.123)

    # Expose /metrics endpoint
    app.add_route("/metrics", metrics_handler)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MetricValue:
    """A single metric value with labels."""
    labels: Dict[str, str]
    value: float
    timestamp: float = field(default_factory=time.time)


class Counter:
    """Prometheus-style counter metric."""

    def __init__(self, name: str, description: str = "", labels: List[str] = None) -> None:
        self.name = name
        self.description = description
        self.label_names = tuple(labels or [])
        self._values: Dict[tuple, float] = defaultdict(float)

    def inc(self, amount: float = 1, **labels) -> None:
        """Increment counter."""
        key = self._make_key(labels)
        self._values[key] += amount

    def _make_key(self, labels: Dict[str, str]) -> tuple:
        """Create hashable key from labels."""
        return tuple(labels.get(name, "") for name in self.label_names)

    def collect(self) -> List[MetricValue]:
        """Collect all values."""
        return [
            MetricValue(labels=dict(zip(self.label_names, key)), value=val)
            for key, val in self._values.items()
        ]


class Gauge:
    """Prometheus-style gauge metric."""

    def __init__(self, name: str, description: str = "", labels: List[str] = None) -> None:
        self.name = name
        self.description = description
        self.label_names = tuple(labels or [])
        self._values: Dict[tuple, float] = {}

    def set(self, value: float, **labels) -> None:
        """Set gauge value."""
        key = self._make_key(labels)
        self._values[key] = value

    def inc(self, amount: float = 1, **labels) -> None:
        """Increment gauge."""
        key = self._make_key(labels)
        self._values[key] = self._values.get(key, 0) + amount

    def dec(self, amount: float = 1, **labels) -> None:
        """Decrement gauge."""
        key = self._make_key(labels)
        self._values[key] = self._values.get(key, 0) - amount

    def _make_key(self, labels: Dict[str, str]) -> tuple:
        return tuple(labels.get(name, "") for name in self.label_names)

    def collect(self) -> List[MetricValue]:
        return [
            MetricValue(labels=dict(zip(self.label_names, key)), value=val)
            for key, val in self._values.items()
        ]


class Histogram:
    """Prometheus-style histogram metric."""

    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

    def __init__(
        self,
        name: str,
        description: str = "",
        labels: List[str] = None,
        buckets: tuple = None,
    ) -> None:
        self.name = name
        self.description = description
        self.label_names = tuple(labels or [])
        self.buckets = buckets or self.DEFAULT_BUCKETS
        # _cumsum[key] = cumulative count
        self._cumsum: Dict[tuple, Dict[float, int]] = defaultdict(lambda: defaultdict(int))
        self._sums: Dict[tuple, float] = defaultdict(float)
        self._counts: Dict[tuple, int] = defaultdict(int)

    def observe(self, value: float, **labels) -> None:
        """Observe a value."""
        key = self._make_key(labels)
        # 修复：删除 `self._cumsum[key][value] += 1`——把每次观测的浮点值当作
        # dict key 存入 cumsum 既是语义错误（cumsum 应只含 bucket 边界累计计数），
        # 又是内存泄漏（观测值种类无限增长）。collect() 只遍历 self.buckets，
        # 这些浮点 key 永远不会被读出。
        self._sums[key] += value
        self._counts[key] += 1

        # Update bucket counts (cumulative)
        for bucket in self.buckets:
            self._cumsum[key][bucket] += 1 if value <= bucket else 0

    def _make_key(self, labels: Dict[str, str]) -> tuple:
        return tuple(labels.get(name, "") for name in self.label_names)

    def collect(self) -> List[MetricValue]:
        """Collect histogram buckets + sum + count."""
        result = []
        for key, buckets in self._cumsum.items():
            labels_dict = dict(zip(self.label_names, key))

            # Bucket values
            for bucket in self.buckets:
                result.append(MetricValue(
                    labels={**labels_dict, "le": str(bucket)},
                    value=float(buckets[bucket]),
                ))

            # +Inf bucket
            result.append(MetricValue(
                labels={**labels_dict, "le": "+Inf"},
                value=float(self._counts[key]),
            ))

            # Sum
            result.append(MetricValue(
                labels=labels_dict,
                value=self._sums[key],
            ))

            # Count
            result.append(MetricValue(
                labels=labels_dict,
                value=float(self._counts[key]),
            ))

        return result


class Summary:
    """Prometheus-style summary metric (quantiles)."""

    def __init__(
        self,
        name: str,
        description: str = "",
        labels: List[str] = None,
        quantiles: tuple = (0.5, 0.9, 0.95, 0.99),
    ) -> None:
        self.name = name
        self.description = description
        self.label_names = tuple(labels or [])
        self.quantiles = quantiles
        self._values: Dict[tuple, List[float]] = defaultdict(list)
        self._sums: Dict[tuple, float] = defaultdict(float)
        self._counts: Dict[tuple, int] = defaultdict(int)

    def observe(self, value: float, **labels) -> None:
        """Observe a value."""
        key = self._make_key(labels)
        self._values[key].append(value)
        self._sums[key] += value
        self._counts[key] += 1

    def _make_key(self, labels: Dict[str, str]) -> tuple:
        return tuple(labels.get(name, "") for name in self.label_names)

    def collect(self) -> List[MetricValue]:
        result = []
        for key, values in self._values.items():
            labels_dict = dict(zip(self.label_names, key))

            if values:
                sorted_values = sorted(values)
                n = len(sorted_values)

                for q in self.quantiles:
                    idx = int(n * q)
                    result.append(MetricValue(
                        labels={**labels_dict, "quantile": str(q)},
                        value=sorted_values[min(idx, n - 1)],
                    ))

            # Sum and count
            result.append(MetricValue(labels=labels_dict, value=self._sums[key]))
            result.append(MetricValue(labels=labels_dict, value=float(self._counts[key])))

        return result


class MetricsRegistry:
    """Central metrics registry with Prometheus format export."""

    def __init__(self) -> None:
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._summaries: Dict[str, Summary] = {}
        self._custom_metrics: Dict[str, Callable] = {}

    def counter(
        self, name: str, description: str = "", labels: List[str] = None
    ) -> Counter:
        """Get or create a counter."""
        if name not in self._counters:
            self._counters[name] = Counter(name, description, labels)
        return self._counters[name]

    def gauge(
        self, name: str, description: str = "", labels: List[str] = None
    ) -> Gauge:
        """Get or create a gauge."""
        if name not in self._gauges:
            self._gauges[name] = Gauge(name, description, labels)
        return self._gauges[name]

    def histogram(
        self, name: str, description: str = "", labels: List[str] = None,
        buckets: tuple = None,
    ) -> Histogram:
        """Get or create a histogram."""
        if name not in self._histograms:
            self._histograms[name] = Histogram(name, description, labels, buckets)
        return self._histograms[name]

    def summary(
        self, name: str, description: str = "", labels: List[str] = None,
        quantiles: tuple = None,
    ) -> Summary:
        """Get or create a summary."""
        if name not in self._summaries:
            self._summaries[name] = Summary(name, description, labels, quantiles)
        return self._summaries[name]

    def register_custom(self, name: str, collector: Callable) -> None:
        """Register a custom metric collector function.

        The collector should return a list of MetricValue objects.
        """
        self._custom_metrics[name] = collector

    def collect_all(self) -> List[tuple]:
        """Collect all metrics as (metric_type, name, description, values)."""
        result = []

        for name, metric in self._counters.items():
            result.append(("counter", metric.name, metric.description, metric.collect()))

        for name, metric in self._gauges.items():
            result.append(("gauge", metric.name, metric.description, metric.collect()))

        for name, metric in self._histograms.items():
            result.append(("histogram", metric.name, metric.description, metric.collect()))

        for name, metric in self._summaries.items():
            result.append(("summary", metric.name, metric.description, metric.collect()))

        for name, collector in self._custom_metrics.items():
            try:
                values = collector()
                result.append(("gauge", name, "", values))
            except Exception as exc:
                logger.warning("Custom metric %s failed: %s", name, exc)

        return result

    def format_prometheus(self) -> str:
        """Format all metrics in Prometheus text format."""
        lines = []

        for mtype, name, desc, values in self.collect_all():
            # Help line
            if desc:
                lines.append(f"# HELP {name} {desc}")
            # Type line
            lines.append(f"# TYPE {name} {mtype}")

            if not values:
                continue

            for mv in values:
                label_str = ""
                if mv.labels:
                    label_parts = [f'{k}="{v}"' for k, v in mv.labels.items()]
                    label_str = "{" + ",".join(label_parts) + "}"

                # Format value
                if mv.value == int(mv.value):
                    value_str = f"{int(mv.value)}"
                else:
                    value_str = f"{mv.value:.6f}"

                lines.append(f"{name}{label_str} {value_str}")

            lines.append("")  # Blank line between metrics

        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all metrics (for testing)."""
        self._counters.clear()
        self._gauges.clear()
        self._histograms.clear()
        self._summaries.clear()


# Singleton
_registry: Optional[MetricsRegistry] = None


def get_metrics_registry() -> MetricsRegistry:
    """Get the shared metrics registry."""
    global _registry
    if _registry is None:
        _registry = MetricsRegistry()
        _setup_default_metrics()
    return _registry


def _setup_default_metrics() -> None:
    """Set up default application metrics."""
    reg = _registry

    # Request metrics
    reg.counter("oneagent_requests_total", "Total requests", ["method", "endpoint", "status"])
    reg.histogram("oneagent_request_duration_seconds", "Request duration", ["method", "endpoint"])
    reg.gauge("oneagent_active_requests", "Active requests in progress", ["endpoint"])

    # LLM metrics
    reg.counter("oneagent_llm_requests_total", "LLM requests", ["provider", "model"])
    reg.histogram("oneagent_llm_duration_seconds", "LLM response time", ["provider", "model"])
    reg.counter("oneagent_tokens_total", "Tokens used", ["provider", "model", "type"])
    reg.counter("oneagent_llm_errors_total", "LLM errors", ["provider", "model", "error_type"])

    # Cache metrics
    reg.counter("oneagent_cache_hits_total", "Cache hits", ["cache_type"])
    reg.counter("oneagent_cache_misses_total", "Cache misses", ["cache_type"])

    # Memory metrics
    reg.gauge("oneagent_memory_entities", "Memory entities", ["memory_type"])
    reg.gauge("oneagent_memory_relations", "Memory relations", ["memory_type"])

    # Skill metrics
    reg.counter("oneagent_skill_usage_total", "Skill usage", ["skill", "status"])

    # Error metrics
    reg.counter("oneagent_errors_total", "Total errors", ["component", "error_type"])

    # Session metrics
    reg.gauge("oneagent_active_sessions", "Active sessions")
    reg.counter("oneagent_sessions_total", "Total sessions", ["status"])


# ===================================================== Built-in collectors

def cpu_memory_collector() -> List[MetricValue]:
    """Collect CPU and memory usage."""
    values = []

    try:
        import psutil
        process = psutil.Process()
        values.append(MetricValue(
            labels={},
            value=process.cpu_percent() / 100,
        ))
        values.append(MetricValue(
            labels={},
            value=process.memory_info().rss / 1024 / 1024,  # MB
        ))
    except ImportError:
        pass

    return values


def format_metrics_handler() -> str:
    """Format handler for /metrics endpoint."""
    reg = get_metrics_registry()

    # Add custom collectors
    reg.register_custom("process_cpu_usage", cpu_memory_collector)

    return reg.format_prometheus()