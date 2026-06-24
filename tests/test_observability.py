"""Tests for observability and ops features: Prometheus, health, tracing, backup."""

import json
import sqlite3
import tempfile
import time
from pathlib import Path


class TestPrometheusMetrics:
    """Test Prometheus metrics exporter."""

    def test_counter(self):
        """Test counter metric."""
        from monitor.prometheus import Counter

        counter = Counter("test_counter", "A test counter", ["method"])
        counter.inc(method="GET")
        counter.inc(method="GET")
        counter.inc(method="POST")

        values = counter.collect()
        assert len(values) == 2  # GET=2, POST=1

        get_val = next(v for v in values if v.labels.get("method") == "GET")
        assert get_val.value == 2

    def test_gauge(self):
        """Test gauge metric."""
        from monitor.prometheus import Gauge

        gauge = Gauge("test_gauge", "A test gauge", ["status"])
        gauge.set(10, status="active")
        gauge.set(5, status="inactive")
        gauge.inc(2, status="active")

        values = gauge.collect()
        active = next(v for v in values if v.labels.get("status") == "active")
        assert active.value == 12

    def test_histogram(self):
        """Test histogram metric."""
        from monitor.prometheus import Histogram

        hist = Histogram("test_histogram", "A test histogram", ["endpoint"])

        for i in range(100):
            hist.observe(0.05 * i, endpoint="/api")

        values = hist.collect()
        # Should have buckets + sum + count
        endpoint_values = [v for v in values if v.labels.get("endpoint") == "/api"]
        assert len(endpoint_values) > 0

    def test_metrics_registry(self):
        """Test metrics registry."""
        from monitor.prometheus import get_metrics_registry

        reg = get_metrics_registry()
        reg.clear()  # Start fresh

        counter = reg.counter("requests", ["method"])
        counter.inc(method="GET")

        # Format as Prometheus text
        output = reg.format_prometheus()
        assert "requests" in output
        assert "# HELP requests" in output
        assert "# TYPE requests counter" in output

    def test_format_prometheus_output(self):
        """Test Prometheus format output."""
        from monitor.prometheus import Counter, get_metrics_registry

        reg = get_metrics_registry()
        reg.clear()

        reg.counter("http_requests_total", "Total HTTP requests", ["status"]).inc(status="200")

        output = reg.format_prometheus()

        assert "http_requests_total{" in output
        assert 'status="200"' in output
        assert "http_requests_total 1.0" in output or "http_requests_total{status=" in output


class TestHealthCheck:
    """Test health check API."""

    def test_liveness_check(self):
        """Test basic liveness check."""
        from monitor.health import get_health_checker, HealthStatus

        checker = get_health_checker()
        result = checker.check_liveness()

        assert "status" in result
        assert result["status"] == HealthStatus.HEALTHY.value
        assert "uptime_seconds" in result
        assert "timestamp" in result

    def test_component_registration(self):
        """Test registering custom health checks."""
        from monitor.health import HealthChecker, ComponentCheck, HealthStatus

        checker = HealthChecker()
        checker.register_check("custom", lambda: ComponentCheck(
            name="custom",
            status=HealthStatus.HEALTHY,
            message="Custom check passed",
        ))

        result = checker.check_all()
        assert "custom" in result["components"]
        assert result["components"]["custom"]["status"] == HealthStatus.HEALTHY.value

    def test_health_status_enum(self):
        """Test health status enum values."""
        from monitor.health import HealthStatus

        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.DEGRADED.value == "degraded"
        assert HealthStatus.UNHEALTHY.value == "unhealthy"


class TestDistributedTracing:
    """Test distributed tracing."""

    def test_span_creation(self):
        """Test creating spans."""
        from monitor.tracing import SimpleTracer

        tracer = SimpleTracer("test-service")
        tracer.clear()

        span = tracer.start_span("test-span")
        assert span.name == "test-span"
        assert span.trace_id != ""
        assert span.span_id != ""

    def test_span_attributes(self):
        """Test span attributes."""
        from monitor.tracing import SimpleTracer

        tracer = SimpleTracer("test-service")
        tracer.clear()

        span = tracer.start_span("test-span")
        span.set_attribute("user.id", "123")
        span.set_attribute("request.size", 1024)

        assert span.attributes["user.id"] == "123"
        assert span.attributes["request.size"] == 1024

    def test_span_context_manager(self):
        """Test span as context manager."""
        from monitor.tracing import SimpleTracer

        tracer = SimpleTracer("test-service")
        tracer.clear()

        with tracer.start_as_current_span("operation") as span:
            span.set_attribute("key", "value")

        # Span should be ended and collected
        assert len(tracer._spans) == 1
        assert tracer._spans[0].name == "operation"
        assert tracer._spans[0].attributes["key"] == "value"
        assert tracer._spans[0].status == "ok"

    def test_span_error_status(self):
        """Test span error status is recorded."""
        from monitor.tracing import SimpleTracer

        tracer = SimpleTracer("test-service")
        tracer.clear()

        try:
            with tracer.start_as_current_span("failing-operation") as span:
                span.set_status("error", "Something went wrong")
                raise ValueError("Test error")
        except ValueError:
            pass  # Expected

        # Span should have error status
        assert len(tracer._spans) >= 1
        last_span = tracer._spans[-1]
        assert last_span.status == "error"
        assert last_span.error_message == "Something went wrong"

    def test_trace_context_headers(self):
        """Test W3C TraceContext headers."""
        from monitor.tracing import TraceContext

        ctx = TraceContext(trace_id="abc123", span_id="def456")
        headers = ctx.to_headers()

        assert "traceparent" in headers
        assert headers["traceparent"].startswith("00-")
        assert "abc123" in headers["traceparent"]

    def test_trace_context_from_headers(self):
        """Test parsing trace context from headers."""
        from monitor.tracing import TraceContext

        headers = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
        ctx = TraceContext.from_headers(headers)

        assert ctx is not None
        assert ctx.trace_id == "0af7651916cd43dd8448eb211c80319c"
        assert ctx.span_id == "b7ad6b7169203331"

    def test_trace_tree_format(self):
        """Test ASCII trace tree formatting."""
        from monitor.tracing import SimpleTracer

        tracer = SimpleTracer("test-service")
        tracer.clear()

        # Create spans using context manager
        with tracer.start_as_current_span("parent-span") as p:
            pass
        with tracer.start_as_current_span("child-span") as c:
            pass

        # Get a trace ID
        trace_id = tracer._spans[0].trace_id if tracer._spans else "none"

        tree = tracer.format_trace_tree(trace_id)
        assert "Trace" in tree

    def test_tracer_stats(self):
        """Test tracer statistics."""
        from monitor.tracing import SimpleTracer

        tracer = SimpleTracer("test-service")
        tracer.clear()

        stats = tracer.get_stats()
        assert stats["service_name"] == "test-service"
        assert stats["enabled"] is True


class TestBackupExport:
    """Test data backup and export."""

    def test_export_sessions_to_json(self, tmp_path):
        """Test exporting sessions to JSON."""
        from core.backup_export import DataExporter

        exporter = DataExporter(data_dir=str(tmp_path))
        data = exporter._export_sessions_to_json()

        # Returns empty dict if no sessions DB exists
        assert isinstance(data, dict)

    def test_export_import_roundtrip(self, tmp_path):
        """Test export and import roundtrip."""
        from core.backup_export import DataExporter, DataImporter, DataType, ExportFormat

        # Create test directories
        export_dir = tmp_path / "export"
        export_dir.mkdir()

        # Create test memory DB
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir(parents=True)

        db_path = memory_dir / "sessions.db"
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at REAL,
                updated_at REAL,
                message_count INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO sessions VALUES ('export-test', 1234567890, 1234567890, 3)"
        )
        conn.commit()
        conn.close()

        # Export
        exporter = DataExporter(data_dir=str(tmp_path))
        output_path = str(export_dir / "backup.json")
        result = exporter.export_data_type(
            DataType.SESSIONS,
            output_path,
        )

        assert result.success is True

        # Full export to ZIP
        result = exporter.export_all(
            str(export_dir / "backup.zip"),
            format=ExportFormat.ZIP,
            include_config=False,
        )

        assert result.success is True
        assert result.size_bytes > 0
        assert "sessions" in result.items_exported

        # Import to a new location
        import_dir = tmp_path / "import"
        import_dir.mkdir()

        # Create empty sessions DB
        new_db = import_dir / "memory" / "sessions.db"
        new_db.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(new_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at REAL,
                updated_at REAL,
                message_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

        importer = DataImporter(data_dir=str(import_dir))
        import_result = importer.import_from_file(result.file_path, merge=True)

        assert import_result.success is True

    def test_export_result_dataclass(self):
        """Test ExportResult dataclass."""
        from core.backup_export import ExportResult

        result = ExportResult(
            success=True,
            format="zip",
            file_path="/tmp/backup.zip",
            size_bytes=1024,
            items_exported={"sessions": 10},
            duration_seconds=1.5,
        )

        assert result.success is True
        assert result.format == "zip"
        assert result.items_exported["sessions"] == 10

    def test_import_result_dataclass(self):
        """Test ImportResult dataclass."""
        from core.backup_export import ImportResult

        result = ImportResult(
            success=True,
            items_imported={"sessions": 5, "memory": 100},
            duration_seconds=2.0,
        )

        assert result.success is True
        assert result.items_imported["sessions"] == 5
