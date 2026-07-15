"""Alerting — 告警体系，支持钉钉/企业微信/邮件/Webhook。

Provides:
  - AlertManager: rule-based alerting with severity levels
  - DingTalk, WeCom, Email, Webhook alert channels
  - Alert throttling (dedup, rate limit)
  - Integration with circuit breaker and monitor
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertChannel(Enum):
    DINGTALK = "dingtalk"
    WECOM = "wecom"
    EMAIL = "email"
    WEBHOOK = "webhook"
    LOG = "log"


@dataclass
class AlertRule:
    """A rule that triggers an alert when a condition is met."""
    name: str
    severity: AlertSeverity = AlertSeverity.WARNING
    condition: Optional[Callable[[Dict[str, Any]], bool]] = None
    channels: List[AlertChannel] = field(default_factory=lambda: [AlertChannel.LOG])
    cooldown_seconds: float = 300.0  # min time between repeat alerts
    message_template: str = "{title}: {body}"


@dataclass
class Alert:
    """An alert instance."""
    rule_name: str
    severity: AlertSeverity
    title: str
    body: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class AlertThrottle:
    """Prevents alert storms by deduplicating and rate-limiting."""

    def __init__(self, max_per_minute: int = 10):
        self._max_per_minute = max_per_minute
        self._recent: Dict[str, float] = {}
        self._lock = threading.Lock()

    def should_send(self, alert_key: str, cooldown: float = 300.0) -> bool:
        """Check if an alert should be sent (not throttled)."""
        now = time.time()
        with self._lock:
            # Clean old entries
            self._recent = {
                k: v for k, v in self._recent.items()
                if now - v < max(cooldown, 3600)
            }

            # Check rate limit
            recent_count = sum(
                1 for v in self._recent.values() if now - v < 60
            )
            if recent_count >= self._max_per_minute:
                return False

            # Check dedup
            if alert_key in self._recent:
                if now - self._recent[alert_key] < cooldown:
                    return False

            self._recent[alert_key] = now
            return True


class AlertManager:
    """Alert rule engine with multiple notification channels.

    Usage:
        mgr = AlertManager()
        mgr.add_rule(AlertRule(
            name="circuit_open",
            severity=AlertSeverity.CRITICAL,
            channels=[AlertChannel.DINGTALK, AlertChannel.LOG],
        ))
        mgr.configure_channel(AlertChannel.DINGTALK, webhook_url="https://...")
        await mgr.fire("circuit_open", title="LLM circuit OPEN", body="...")
    """

    def __init__(self):
        self._rules: Dict[str, AlertRule] = {}
        self._channel_configs: Dict[AlertChannel, Dict[str, Any]] = {
            AlertChannel.DINGTALK: {},
            AlertChannel.WECOM: {},
            AlertChannel.EMAIL: {},
            AlertChannel.WEBHOOK: {},
            AlertChannel.LOG: {},
        }
        self._throttle = AlertThrottle()
        self._stats = {"total": 0, "throttled": 0, "sent": 0}
        self._lock = threading.Lock()

    def add_rule(self, rule: AlertRule) -> None:
        """Register an alert rule."""
        self._rules[rule.name] = rule

    def remove_rule(self, name: str) -> None:
        self._rules.pop(name, None)

    def configure_channel(
        self, channel: AlertChannel, **config,
    ) -> None:
        """Configure an alert channel.

        DingTalk: webhook_url, secret (optional)
        WeCom: webhook_url
        Email: smtp_host, smtp_port, username, password, to_addrs
        Webhook: url, method, headers
        """
        self._channel_configs[channel].update(config)

    async def fire(
        self,
        rule_name: str,
        title: str,
        body: str = "",
        severity: Optional[AlertSeverity] = None,
        metadata: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> bool:
        """Fire an alert.

        Returns True if the alert was sent, False if throttled.
        """
        rule = self._rules.get(rule_name)
        if rule is None:
            logger.warning("alert: unknown rule '%s'", rule_name)
            return False

        alert = Alert(
            rule_name=rule_name,
            severity=severity or rule.severity,
            title=title,
            body=body,
            metadata=metadata or {},
        )

        with self._lock:
            self._stats["total"] += 1

        # Throttle check
        if not force and not self._throttle.should_send(
            f"{rule_name}:{title}", rule.cooldown_seconds,
        ):
            with self._lock:
                self._stats["throttled"] += 1
            logger.debug("alert '%s' throttled", rule_name)
            return False

        with self._lock:
            self._stats["sent"] += 1

        message = rule.message_template.format(
            title=title, body=body, severity=alert.severity.value,
        )

        # Send to all configured channels
        tasks = []
        for channel in rule.channels:
            tasks.append(self._send(channel, alert, message))

        # 修复：channels 为空时 all([]) 返回 True，误报"发送成功"。
        # 空渠道列表意味着告警实际未送达任何目的地，应返回 False。
        if not tasks:
            logger.warning("alert rule %s has no channels, alert not sent", rule.name)
            return False

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = all(not isinstance(r, Exception) for r in results)
        return success

    async def _send(
        self, channel: AlertChannel, alert: Alert, message: str,
    ) -> None:
        """Send an alert through a specific channel."""
        config = self._channel_configs.get(channel, {})

        if channel == AlertChannel.LOG:
            level = {
                AlertSeverity.INFO: logging.INFO,
                AlertSeverity.WARNING: logging.WARNING,
                AlertSeverity.CRITICAL: logging.ERROR,
            }.get(alert.severity, logging.WARNING)
            logger.log(level, "[ALERT %s] %s: %s", alert.severity.value.upper(), alert.title, alert.body)
            return

        if channel == AlertChannel.DINGTALK:
            await self._send_dingtalk(alert, message, config)
        elif channel == AlertChannel.WECOM:
            await self._send_wecom(alert, message, config)
        elif channel == AlertChannel.EMAIL:
            await self._send_email(alert, message, config)
        elif channel == AlertChannel.WEBHOOK:
            await self._send_webhook(alert, message, config)

    async def _send_dingtalk(
        self, alert: Alert, message: str, config: Dict[str, Any],
    ) -> None:
        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            return

        import httpx

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"[{alert.severity.value.upper()}] {alert.title}",
                "text": (
                    f"## [{alert.severity.value.upper()}] {alert.title}\n\n"
                    f"{alert.body}\n\n"
                    f"> 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"> 规则: {alert.rule_name}"
                ),
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code != 200:
                    logger.warning("dingtalk alert failed: %d %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("dingtalk alert error: %s", exc)

    async def _send_wecom(
        self, alert: Alert, message: str, config: Dict[str, Any],
    ) -> None:
        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            return

        import httpx

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": (
                    f"## [{alert.severity.value.upper()}] {alert.title}\n"
                    f"{alert.body}\n"
                    f"> 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                ),
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code != 200:
                    logger.warning("wecom alert failed: %d %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("wecom alert error: %s", exc)

    async def _send_email(
        self, alert: Alert, message: str, config: Dict[str, Any],
    ) -> None:
        smtp_host = config.get("smtp_host", "")
        if not smtp_host:
            return

        import smtplib
        from email.mime.text import MIMEText

        # 构造消息对象可在当前协程完成（无 IO）
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = f"[{alert.severity.value.upper()}] {alert.title}"
        msg["From"] = config.get("username", "one-agent@localhost")
        msg["To"] = ", ".join(config.get("to_addrs", []))

        def _smtp_send() -> None:
            # smtplib 是同步阻塞库，TCP 连接 + TLS 握手 + 认证 + 发送
            # 全程阻塞事件循环。若 SMTP 服务器慢或不可达，整个 agent 卡死。
            # 用 asyncio.to_thread 把阻塞调用丢到线程池执行。
            smtp = smtplib.SMTP(smtp_host, int(config.get("smtp_port", 587)))
            try:
                smtp.starttls()
                smtp.login(config.get("username", ""), config.get("password", ""))
                smtp.send_message(msg)
            finally:
                smtp.quit()

        try:
            import asyncio
            await asyncio.to_thread(_smtp_send)
            logger.info("email alert sent: %s", alert.title)
        except Exception as exc:
            logger.warning("email alert failed: %s", exc)

    async def _send_webhook(
        self, alert: Alert, message: str, config: Dict[str, Any],
    ) -> None:
        url = config.get("url", "")
        if not url:
            return

        import httpx

        method = config.get("method", "POST").upper()
        headers = config.get("headers", {})
        payload = {
            "alert": alert.rule_name,
            "severity": alert.severity.value,
            "title": alert.title,
            "body": alert.body,
            "timestamp": alert.timestamp,
            "metadata": alert.metadata,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if method == "POST":
                    resp = await client.post(url, json=payload, headers=headers)
                else:
                    resp = await client.put(url, json=payload, headers=headers)
                if resp.status_code >= 400:
                    logger.warning("webhook alert failed: %d", resp.status_code)
        except Exception as exc:
            logger.warning("webhook alert error: %s", exc)

    # --------------------------------------------------- built-in rules

    def setup_default_rules(self) -> None:
        """Register default alert rules for common scenarios."""
        self.add_rule(AlertRule(
            name="circuit_open",
            severity=AlertSeverity.CRITICAL,
            channels=[AlertChannel.DINGTALK, AlertChannel.LOG],
            cooldown_seconds=600,
            message_template="熔断器触发: {title}\n{body}",
        ))
        self.add_rule(AlertRule(
            name="circuit_closed",
            severity=AlertSeverity.INFO,
            channels=[AlertChannel.LOG],
            cooldown_seconds=300,
            message_template="熔断器恢复: {title}",
        ))
        self.add_rule(AlertRule(
            name="llm_error",
            severity=AlertSeverity.WARNING,
            channels=[AlertChannel.DINGTALK, AlertChannel.LOG],
            cooldown_seconds=300,
            message_template="LLM 调用失败: {title}\n{body}",
        ))
        self.add_rule(AlertRule(
            name="high_error_rate",
            severity=AlertSeverity.CRITICAL,
            channels=[AlertChannel.DINGTALK, AlertChannel.LOG],
            cooldown_seconds=900,
            message_template="错误率过高: {title}\n{body}",
        ))
        self.add_rule(AlertRule(
            name="rate_limit_hit",
            severity=AlertSeverity.WARNING,
            channels=[AlertChannel.LOG],
            cooldown_seconds=120,
            message_template="速率限制触发: {title}",
        ))
        self.add_rule(AlertRule(
            name="memory_high_usage",
            severity=AlertSeverity.WARNING,
            channels=[AlertChannel.LOG],
            cooldown_seconds=600,
            message_template="内存使用率高: {title}\n{body}",
        ))
        self.add_rule(AlertRule(
            name="webhook_failure",
            severity=AlertSeverity.WARNING,
            channels=[AlertChannel.LOG],
            cooldown_seconds=300,
            message_template="Webhook 失败: {title}\n{body}",
        ))

    # --------------------------------------------------- context integration

    def check_and_alert(
        self, metrics: Dict[str, Any],
    ) -> List[str]:
        """Check metrics against rules and fire alerts as needed.

        Returns list of rule names that were triggered.
        """
        triggered = []

        # Check circuit states
        circuits = metrics.get("circuits", {}).get("circuits", [])
        for c in circuits:
            if c.get("state") == "open":
                triggered.append("circuit_open")

        # Check error rate
        request_stats = metrics.get("request_stats", {})
        error_rate = request_stats.get("error_rate", 0)
        if error_rate > 0.1:  # >10% error rate
            triggered.append("high_error_rate")

        return triggered

    def get_stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def sync_channels_from_plugin(self, plugin_alert_manager) -> None:
        """Sync channel configuration from the Plugin-based AlertManager.

        This bridges the gap between the core.alerting singleton (used by
        coordinator) and the alerting.Plugin (configured from YAML).
        After calling this, the singleton will send alerts to the same
        channels as the plugin.
        """
        try:
            # Copy raw channel configs if available
            raw_cfg = getattr(plugin_alert_manager, "_raw_channel_configs", None)
            if raw_cfg:
                for ch in raw_cfg:
                    ch_type = ch.get("type", "")
                    if ch_type == "webhook":
                        self.configure_channel(
                            AlertChannel.WEBHOOK,
                            url=ch.get("url", ""),
                            method=ch.get("method", "POST"),
                            headers=ch.get("headers", {}),
                        )
                    elif ch_type == "slack":
                        self.configure_channel(
                            AlertChannel.WECOM,
                            webhook_url=ch.get("webhook_url", ch.get("url", "")),
                        )
                    elif ch_type == "dingtalk":
                        self.configure_channel(
                            AlertChannel.DINGTALK,
                            webhook_url=ch.get("webhook_url", ch.get("url", "")),
                        )
            logger.info("alerting: synced channels from plugin")
        except Exception as exc:
            logger.warning("alerting: sync_channels failed: %s", exc)


# Singleton
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """Get or create the shared AlertManager."""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
        _alert_manager.setup_default_rules()
    return _alert_manager