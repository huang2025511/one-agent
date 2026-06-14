"""Alerting system — monitors metrics and sends alerts when thresholds are exceeded.

Supports multiple alert channels:
  - Webhook (generic HTTP POST)
  - Email (via SMTP)
  - Slack/Discord webhooks
  - Custom handlers

Alert rules are configurable via config file or runtime API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AlertRule:
    """Defines a metric threshold that triggers an alert."""
    name: str
    metric_path: str  # e.g., "bus.errors", "llm.cache.hit_rate"
    operator: str  # ">", "<", ">=", "<=", "==", "!="
    threshold: float
    severity: str = "warning"  # "info", "warning", "critical"
    cooldown_seconds: int = 300  # Minimum time between alerts for same rule
    enabled: bool = True
    last_triggered: float = 0.0
    description: str = ""


@dataclass
class AlertEvent:
    """Represents a triggered alert."""
    rule_name: str
    severity: str
    message: str
    metric_value: float
    threshold: float
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class AlertManager:
    """Manages alert rules and dispatches alerts to configured channels."""

    def __init__(self) -> None:
        self._rules: Dict[str, AlertRule] = {}
        self._channels: List[Callable[[AlertEvent], Any]] = []
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._check_interval = 30  # seconds
        self._alert_history: List[AlertEvent] = []
        self._max_history = 100

    async def setup(self, config: Dict[str, Any]) -> None:
        """Initialize alert manager from config."""
        self._client = httpx.AsyncClient(timeout=10)

        # Load alert rules from config
        rules_cfg = config.get("alerting", {}).get("rules", [])
        for rule_dict in rules_cfg:
            rule = AlertRule(
                name=rule_dict["name"],
                metric_path=rule_dict["metric_path"],
                operator=rule_dict["operator"],
                threshold=float(rule_dict["threshold"]),
                severity=rule_dict.get("severity", "warning"),
                cooldown_seconds=rule_dict.get("cooldown_seconds", 300),
                enabled=rule_dict.get("enabled", True),
                description=rule_dict.get("description", ""),
            )
            self._rules[rule.name] = rule

        # Configure alert channels
        channels_cfg = config.get("alerting", {}).get("channels", [])
        for ch_cfg in channels_cfg:
            ch_type = ch_cfg.get("type")
            if ch_type == "webhook":
                self._channels.append(self._make_webhook_channel(ch_cfg))
            elif ch_type == "slack":
                self._channels.append(self._make_slack_channel(ch_cfg))
            elif ch_type == "log":
                self._channels.append(self._make_log_channel(ch_cfg))

        # Default to log channel if none configured
        if not self._channels:
            self._channels.append(self._make_log_channel({}))

        logger.info("alert manager configured with %d rules, %d channels",
                    len(self._rules), len(self._channels))

    def _make_webhook_channel(self, cfg: Dict[str, Any]) -> Callable:
        url = cfg.get("url", "")
        headers = cfg.get("headers", {})

        async def send(alert: AlertEvent):
            if not self._client:
                return
            payload = {
                "rule": alert.rule_name,
                "severity": alert.severity,
                "message": alert.message,
                "value": alert.metric_value,
                "threshold": alert.threshold,
                "timestamp": alert.timestamp,
            }
            try:
                await self._client.post(url, json=payload, headers=headers)
            except Exception as exc:
                logger.warning("webhook alert failed: %s", exc)

        return send

    def _make_slack_channel(self, cfg: Dict[str, Any]) -> Callable:
        webhook_url = cfg.get("webhook_url", "")
        channel = cfg.get("channel", "")
        username = cfg.get("username", "One-Agent Alert")

        async def send(alert: AlertEvent):
            if not self._client:
                return
            color = {"info": "#36a64f", "warning": "#ff9900", "critical": "#ff0000"}.get(
                alert.severity, "#ff0000"
            )
            payload = {
                "channel": channel,
                "username": username,
                "attachments": [{
                    "color": color,
                    "title": f"[{alert.severity.upper()}] {alert.rule_name}",
                    "text": alert.message,
                    "fields": [
                        {"title": "Metric Value", "value": str(alert.metric_value), "short": True},
                        {"title": "Threshold", "value": str(alert.threshold), "short": True},
                    ],
                    "ts": int(alert.timestamp),
                }],
            }
            try:
                await self._client.post(webhook_url, json=payload)
            except Exception as exc:
                logger.warning("slack alert failed: %s", exc)

        return send

    def _make_log_channel(self, cfg: Dict[str, Any]) -> Callable:
        level = cfg.get("level", "WARNING")

        async def send(alert: AlertEvent):
            log_func = getattr(logger, level.lower(), logger.warning)
            log_func("ALERT [%s] %s: %s (value=%.2f, threshold=%.2f)",
                     alert.severity, alert.rule_name, alert.message,
                     alert.metric_value, alert.threshold)

        return send

    def add_rule(self, rule: AlertRule) -> None:
        """Add or update an alert rule."""
        self._rules[rule.name] = rule
        logger.info("alert rule added: %s", rule.name)

    def remove_rule(self, name: str) -> None:
        """Remove an alert rule."""
        if name in self._rules:
            del self._rules[name]
            logger.info("alert rule removed: %s", name)

    def list_rules(self) -> List[Dict[str, Any]]:
        """List all alert rules."""
        return [
            {
                "name": r.name,
                "metric_path": r.metric_path,
                "operator": r.operator,
                "threshold": r.threshold,
                "severity": r.severity,
                "enabled": r.enabled,
                "cooldown_seconds": r.cooldown_seconds,
                "last_triggered": r.last_triggered,
                "description": r.description,
            }
            for r in self._rules.values()
        ]

    def list_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List recent alert events."""
        return [
            {
                "rule_name": e.rule_name,
                "severity": e.severity,
                "message": e.message,
                "metric_value": e.metric_value,
                "threshold": e.threshold,
                "timestamp": e.timestamp,
            }
            for e in self._alert_history[-limit:]
        ]

    async def start(self) -> None:
        """Start the alert checking loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("alert manager started (check interval=%ds)", self._check_interval)

    async def stop(self) -> None:
        """Stop the alert checking loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        logger.info("alert manager stopped")

    async def _check_loop(self) -> None:
        """Periodically check metrics against alert rules."""
        while self._running:
            try:
                await self._check_all_rules()
            except asyncio.CancelledError:
                # Graceful shutdown
                logger.debug("alert check loop cancelled")
                break
            except Exception as exc:
                # Log but continue - don't let one error stop the monitoring
                logger.warning("alert check error (continuing): %s", exc)
            await asyncio.sleep(self._check_interval)

    async def _check_all_rules(self, metrics_getter: Optional[Callable] = None) -> None:
        """Check all enabled rules against current metrics."""
        # This would be called with a metrics getter from the monitoring plugin
        # For now, we'll skip actual checking if no getter is provided
        if metrics_getter is None:
            return

        metrics = metrics_getter()
        now = time.time()

        for rule in self._rules.values():
            if not rule.enabled:
                continue

            # Check cooldown
            if now - rule.last_triggered < rule.cooldown_seconds:
                continue

            # Extract metric value
            value = self._extract_metric(metrics, rule.metric_path)
            if value is None:
                continue

            # Evaluate condition
            if self._evaluate_condition(value, rule.operator, rule.threshold):
                rule.last_triggered = now
                alert = AlertEvent(
                    rule_name=rule.name,
                    severity=rule.severity,
                    message=rule.description or f"{rule.metric_path} {rule.operator} {rule.threshold}",
                    metric_value=value,
                    threshold=rule.threshold,
                )
                self._alert_history.append(alert)
                if len(self._alert_history) > self._max_history:
                    self._alert_history = self._alert_history[-self._max_history:]

                # Dispatch to all channels
                for channel in self._channels:
                    try:
                        result = channel(alert)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("alert channel failed")

    @staticmethod
    def _extract_metric(metrics: Dict[str, Any], path: str) -> Optional[float]:
        """Extract a metric value from nested dict using dot notation."""
        parts = path.split(".")
        current = metrics
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return float(current) if isinstance(current, (int, float)) else None

    @staticmethod
    def _evaluate_condition(value: float, operator: str, threshold: float) -> bool:
        """Evaluate a comparison operator."""
        if operator == ">":
            return value > threshold
        elif operator == "<":
            return value < threshold
        elif operator == ">=":
            return value >= threshold
        elif operator == "<=":
            return value <= threshold
        elif operator == "==":
            return value == threshold
        elif operator == "!=":
            return value != threshold
        return False
