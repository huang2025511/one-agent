"""Webhook 系统 — 支持事件订阅和自定义 Webhook 端点。

提供：
  - 事件订阅管理
  - 自定义 Webhook 端点配置
  - 消息签名验证
  - 重试机制
  - 死信队列处理
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class WebhookEndpoint:
    """Webhook 端点配置。"""
    endpoint_id: str
    url: str
    events: List[str]  # 订阅的事件类型
    secret: str = ""  # 签名密钥
    enabled: bool = True
    max_retries: int = 3
    timeout: int = 30
    created_at: float = field(default_factory=time.time)
    last_triggered: float = 0.0
    trigger_count: int = 0


@dataclass
class WebhookEvent:
    """Webhook 事件定义。"""
    event_id: str
    event_type: str
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    status: str = "pending"  # pending / sent / failed / retried


@dataclass
class WebhookResponse:
    """Webhook 响应结果。"""
    endpoint_id: str
    event_id: str
    success: bool
    status_code: int = 0
    message: str = ""
    retry_count: int = 0


class WebhookManager:
    """Webhook 管理器 — 处理事件订阅和消息推送。"""

    def __init__(self):
        self._endpoints: Dict[str, WebhookEndpoint] = {}
        self._pending_events: List[WebhookEvent] = []
        self._dead_letter_queue: List[WebhookEvent] = []
        self._client: Optional[httpx.AsyncClient] = None
        self._running = False
        self._process_task: Optional[asyncio.Task] = None

    async def setup(self) -> None:
        """初始化 Webhook 管理器。"""
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30))
        logger.info("Webhook manager initialized")

    async def start(self) -> None:
        """启动事件处理循环。"""
        if self._running:
            return
        self._running = True
        self._process_task = asyncio.create_task(self._process_events())
        logger.info("Webhook manager started")

    async def stop(self) -> None:
        """停止事件处理循环。"""
        self._running = False
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        logger.info("Webhook manager stopped")

    def add_endpoint(
        self,
        endpoint_id: str,
        url: str,
        events: List[str],
        secret: str = "",
        max_retries: int = 3,
        timeout: int = 30,
    ) -> bool:
        """添加 Webhook 端点。"""
        if endpoint_id in self._endpoints:
            logger.warning("Endpoint %s already exists", endpoint_id)
            return False

        self._endpoints[endpoint_id] = WebhookEndpoint(
            endpoint_id=endpoint_id,
            url=url,
            events=events,
            secret=secret,
            max_retries=max_retries,
            timeout=timeout,
        )
        logger.info("Webhook endpoint added: %s -> %s", endpoint_id, url)
        return True

    def remove_endpoint(self, endpoint_id: str) -> bool:
        """移除 Webhook 端点。"""
        if endpoint_id not in self._endpoints:
            return False
        del self._endpoints[endpoint_id]
        logger.info("Webhook endpoint removed: %s", endpoint_id)
        return True

    def get_endpoint(self, endpoint_id: str) -> Optional[WebhookEndpoint]:
        """获取端点配置。"""
        return self._endpoints.get(endpoint_id)

    def list_endpoints(self) -> List[Dict[str, Any]]:
        """列出所有端点。"""
        return [
            {
                "endpoint_id": e.endpoint_id,
                "url": e.url,
                "events": e.events,
                "enabled": e.enabled,
                "trigger_count": e.trigger_count,
                "last_triggered": e.last_triggered,
            }
            for e in self._endpoints.values()
        ]

    def trigger_event(self, event_type: str, payload: Dict[str, Any]) -> str:
        """触发事件 — 将事件加入队列等待处理。"""
        event = WebhookEvent(
            event_id=f"evt_{int(time.time())}_{hash(str(payload)) % 100000}",
            event_type=event_type,
            payload=payload,
        )
        self._pending_events.append(event)
        logger.debug("Event triggered: %s -> %s", event_type, event.event_id)
        return event.event_id

    async def _process_events(self) -> None:
        """事件处理主循环。"""
        while self._running:
            while self._pending_events:
                event = self._pending_events.pop(0)
                await self._dispatch_event(event)
            await asyncio.sleep(1)

    async def _dispatch_event(self, event: WebhookEvent) -> None:
        """分发事件到订阅的端点。"""
        tasks = []
        for endpoint in self._endpoints.values():
            if not endpoint.enabled:
                continue
            if event.event_type not in endpoint.events:
                continue
            tasks.append(self._send_to_endpoint(endpoint, event))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_to_endpoint(
        self,
        endpoint: WebhookEndpoint,
        event: WebhookEvent,
        retry_count: int = 0,
    ) -> WebhookResponse:
        """向端点发送事件。"""
        if retry_count >= endpoint.max_retries:
            self._dead_letter_queue.append(event)
            logger.error("Event %s failed after %d retries, added to dead letter queue",
                        event.event_id, endpoint.max_retries)
            return WebhookResponse(
                endpoint_id=endpoint.endpoint_id,
                event_id=event.event_id,
                success=False,
                message="Max retries exceeded",
                retry_count=retry_count,
            )

        try:
            headers = {
                "Content-Type": "application/json",
                "X-Event-Type": event.event_type,
                "X-Event-ID": event.event_id,
                "X-Timestamp": str(event.timestamp),
            }

            # 添加签名
            if endpoint.secret:
                signature = self._sign_payload(event.payload, endpoint.secret)
                headers["X-Signature"] = signature

            response = await self._client.post(
                endpoint.url,
                headers=headers,
                json=event.payload,
                timeout=endpoint.timeout,
            )

            response.raise_for_status()

            endpoint.last_triggered = time.time()
            endpoint.trigger_count += 1

            logger.debug("Event %s sent to %s successfully", event.event_id, endpoint.endpoint_id)
            return WebhookResponse(
                endpoint_id=endpoint.endpoint_id,
                event_id=event.event_id,
                success=True,
                status_code=response.status_code,
                retry_count=retry_count,
            )

        except Exception as exc:
            logger.warning("Event %s failed to send to %s (attempt %d): %s",
                          event.event_id, endpoint.endpoint_id, retry_count + 1, exc)

            # 重试
            await asyncio.sleep(2 ** retry_count)
            return await self._send_to_endpoint(endpoint, event, retry_count + 1)

    def _sign_payload(self, payload: Dict[str, Any], secret: str) -> str:
        """使用 HMAC-SHA256 签名 payload。"""
        payload_str = json.dumps(payload, separators=(',', ':'), sort_keys=True)
        signature = hmac.new(
            secret.encode(),
            payload_str.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"

    def verify_signature(self, payload: Dict[str, Any], signature: str, secret: str) -> bool:
        """验证签名是否有效。"""
        expected_signature = self._sign_payload(payload, secret)
        return hmac.compare_digest(expected_signature, signature)

    def list_pending_events(self) -> List[Dict[str, Any]]:
        """列出待处理事件。"""
        return [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "timestamp": e.timestamp,
                "status": e.status,
            }
            for e in self._pending_events
        ]

    def list_dead_letter_queue(self) -> List[Dict[str, Any]]:
        """列出死信队列中的事件。"""
        return [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "timestamp": e.timestamp,
                "status": e.status,
            }
            for e in self._dead_letter_queue
        ]

    def retry_dead_letter_events(self) -> int:
        """重试死信队列中的事件。"""
        count = 0
        while self._dead_letter_queue:
            event = self._dead_letter_queue.pop(0)
            event.status = "pending"
            self._pending_events.append(event)
            count += 1
        logger.info("Retried %d events from dead letter queue", count)
        return count

    def clear_dead_letter_queue(self) -> int:
        """清空死信队列。"""
        count = len(self._dead_letter_queue)
        self._dead_letter_queue.clear()
        logger.info("Cleared %d events from dead letter queue", count)
        return count

    # ============================================================
    # 支持的事件类型
    # ============================================================

    # 系统事件
    SYSTEM_EVENTS = [
        "system.startup",
        "system.shutdown",
        "system.config_changed",
        "system.health_status",
    ]

    # 聊天事件
    CHAT_EVENTS = [
        "chat.message_received",
        "chat.message_sent",
        "chat.session_started",
        "chat.session_ended",
    ]

    # 技能事件
    SKILL_EVENTS = [
        "skill.installed",
        "skill.uninstalled",
        "skill.updated",
        "skill.rated",
    ]

    # 任务事件
    TASK_EVENTS = [
        "task.created",
        "task.started",
        "task.completed",
        "task.failed",
        "task.cancelled",
        "task.progress",
    ]

    # 告警事件
    ALERT_EVENTS = [
        "alert.triggered",
        "alert.resolved",
    ]

    # 工作流事件
    WORKFLOW_EVENTS = [
        "workflow.started",
        "workflow.completed",
        "workflow.failed",
        "workflow.step_executed",
    ]

    @classmethod
    def get_all_event_types(cls) -> List[str]:
        """获取所有支持的事件类型。"""
        return (
            cls.SYSTEM_EVENTS
            + cls.CHAT_EVENTS
            + cls.SKILL_EVENTS
            + cls.TASK_EVENTS
            + cls.ALERT_EVENTS
            + cls.WORKFLOW_EVENTS
        )