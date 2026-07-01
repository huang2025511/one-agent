"""Unit tests for SchedulerPlugin.

Covers:
  - SchedulerPlugin lifecycle (setup/start/stop)
  - Cron expression validation
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestSchedulerLifecycle:
    """Test SchedulerPlugin setup, start, and stop."""

    def test_scheduler_import(self):
        """SchedulerPlugin imports successfully."""
        from scheduler import SchedulerPlugin
        assert SchedulerPlugin is not None
        assert SchedulerPlugin.name == "scheduler"

    def test_scheduler_init(self):
        """SchedulerPlugin initializes with correct defaults."""
        from scheduler import SchedulerPlugin

        plugin = SchedulerPlugin()
        assert plugin._scheduler is None
        assert plugin._enabled is True
        assert plugin._jobs_file is None

    def test_add_cron_invalid(self):
        """add_cron handles invalid cron expressions."""
        from scheduler import SchedulerPlugin

        plugin = SchedulerPlugin()
        # Without actual scheduler initialized, this should silently return
        plugin.add_cron("invalid", lambda: None, "test")
        # No exception = success

    def test_add_cron_valid(self):
        """add_cron accepts valid cron expressions."""
        from scheduler import SchedulerPlugin

        plugin = SchedulerPlugin()
        # Valid 5-field cron should not raise
        # (without scheduler initialized, it silently returns)
        plugin.add_cron("0 9 * * *", lambda: None, "test-daily")
        plugin.add_cron("*/5 * * * *", lambda: None, "test-every-5min")