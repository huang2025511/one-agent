"""Unit tests for AlertManager plugin.

Covers:
  - AlertManager lifecycle (setup/start/stop)
  - AlertRule dataclass
  - AlertEvent dataclass
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestAlertManagerLifecycle:
    """Test AlertManager setup, start, and stop."""

    def test_alert_manager_import(self):
        """AlertManager imports successfully."""
        from alerting import AlertManager, AlertRule, AlertEvent
        assert AlertManager is not None
        assert AlertRule is not None
        assert AlertEvent is not None
        assert AlertManager.name == "alerting"

    def test_alert_rule_defaults(self):
        """AlertRule has correct defaults."""
        from alerting import AlertRule

        rule = AlertRule(name="test", metric_path="test.metric", operator=">", threshold=10.0)
        assert rule.name == "test"
        assert rule.metric_path == "test.metric"
        assert rule.operator == ">"
        assert rule.threshold == 10.0
        assert rule.severity == "warning"
        assert rule.cooldown_seconds == 300
        assert rule.enabled is True
        assert rule.last_triggered == 0.0
        assert rule.description == ""

    def test_alert_event_defaults(self):
        """AlertEvent has correct defaults."""
        from alerting import AlertEvent

        event = AlertEvent(rule_name="test", severity="warning", message="test message", metric_value=15.0, threshold=10.0)
        assert event.rule_name == "test"
        assert event.severity == "warning"
        assert event.message == "test message"
        assert event.metric_value == 15.0
        assert event.threshold == 10.0
        assert isinstance(event.timestamp, float)
        assert event.metadata == {}

    def test_alert_manager_init(self):
        """AlertManager initializes with correct defaults."""
        from alerting import AlertManager

        manager = AlertManager()
        assert manager._rules == {}
        assert manager._channels == []
        assert manager._check_interval == 30
        assert manager._max_history == 100
        assert manager._metrics_getter is None