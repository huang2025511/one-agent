"""Unit tests for MonitoringPlugin.

Covers:
  - MonitoringPlugin lifecycle (setup/start/stop)
  - _tail_file helper function
  - Metrics recording
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestMonitoringLifecycle:
    """Test MonitoringPlugin setup, start, and stop."""

    def test_monitor_import(self):
        """MonitoringPlugin imports successfully."""
        from monitor import MonitoringPlugin, _tail_file
        assert MonitoringPlugin is not None
        assert _tail_file is not None
        assert MonitoringPlugin.name == "monitoring"

    def test_monitor_init(self):
        """MonitoringPlugin initializes with correct defaults."""
        from monitor import MonitoringPlugin

        plugin = MonitoringPlugin()
        assert plugin._port == 18793
        assert plugin._enabled is True
        assert plugin._task is None
        assert plugin._request_count == 0
        assert plugin._error_count == 0
        assert plugin._total_tokens == 0


class TestMonitorTailFile:
    """Test _tail_file helper function."""

    def test_tail_file_small(self):
        """Tail small file returns all content."""
        from monitor import _tail_file

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("line1\nline2\nline3\n")
            f.flush()

        result = _tail_file(f.name, max_lines=10)
        assert result == "line1\nline2\nline3"
        import os
        os.unlink(f.name)

    def test_tail_file_large(self):
        """Tail large file returns only last N lines."""
        from monitor import _tail_file

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            for i in range(100):
                f.write(f"line{i}\n")
            f.flush()

        result = _tail_file(f.name, max_lines=5)
        lines = result.split("\n")
        assert len(lines) == 5
        assert lines[0] == "line95"
        import os
        os.unlink(f.name)

    def test_tail_file_empty(self):
        """Tail empty file returns empty string."""
        from monitor import _tail_file

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.flush()

        result = _tail_file(f.name)
        assert result == ""
        import os
        os.unlink(f.name)


class TestMonitorMetrics:
    """Test metrics recording."""

    def test_record_latency(self):
        """Latency recording updates histogram."""
        from monitor import MonitoringPlugin

        plugin = MonitoringPlugin()
        plugin.record_request_latency(0.3)  # Falls in bucket 0.5
        plugin.record_request_latency(1.5)  # Falls in bucket 2.0
        plugin.record_request_latency(0.05)  # Falls in bucket 0.1

        assert plugin._request_count == 3
        # Check bucket counts
        assert plugin._latency_counts[0] == 1  # 0.1
        assert plugin._latency_counts[1] == 1  # 0.5
        assert plugin._latency_counts[3] == 1  # 2.0

    def test_record_tokens(self):
        """Token recording accumulates total."""
        from monitor import MonitoringPlugin

        plugin = MonitoringPlugin()
        plugin.record_token_usage(100)
        plugin.record_token_usage(200)

        assert plugin._total_tokens == 300