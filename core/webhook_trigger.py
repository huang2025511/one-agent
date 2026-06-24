"""Webhook Event Trigger — automate workflows based on events.

Trigger external actions when events occur:
- Send HTTP POST to webhooks on events
- Template-based payloads
- Retry with exponential backoff
- Event filtering by conditions
- Authentication support (API keys, Bearer tokens)
- Rate limiting per endpoint

Example use cases:
- Send Slack notification when task completes
- Trigger CI/CD pipeline on code changes
- Log events to external monitoring systems
- Sync data to external services
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


class WebhookMethod(Enum):
    """HTTP methods for webhooks."""
    POST = "POST"
    GET = "GET"
    PUT = "PUT"


class WebhookAuth(Enum):
    """Authentication methods."""
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    HMAC_SHA256 = "hmac_sha256"


@dataclass
class Webhook:
    """A webhook configuration."""
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    name: str = ""
    url: str = ""
    method: str = WebhookMethod.POST.value
    auth_type: str = WebhookAuth.NONE.value
    # Auth credentials (stored encrypted in production)
    api_key: str = ""
    secret_key: str = ""
    # Payload template
    payload_template: str = "{}"
    # Event filter
    event_filter: str = ""  # JSONLogic-style filter
    # Rate limiting
    rate_limit: int = 10  # Max requests per minute
    enabled: bool = True
    # Retry config
    max_retries: int = 3
    retry_delay: float = 1.0
    # Stats
    created_at: float = field(default_factory=time.time)
    last_triggered: float = 0
    success_count: int = 0
    failure_count: int = 0
    last_error: str = ""


class WebhookTrigger:
    """Manage and trigger webhooks based on events."""

    def __init__(
        self,
        timeout: float = 30.0,
        max_concurrent: int = 5,
    ) -> None:
        self._webhooks: Dict[str, Webhook] = {}
        self._rate_limiters: Dict[str, List[float]] = {}
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout)
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def register(self, webhook: Webhook) -> None:
        """Register a webhook."""
        self._webhooks[webhook.id] = webhook
        logger.info("Registered webhook: %s (%s)", webhook.name, webhook.url)

    def unregister(self, webhook_id: str) -> bool:
        """Unregister a webhook."""
        if webhook_id in self._webhooks:
            del self._webhooks[webhook_id]
            self._rate_limiters.pop(webhook_id, None)
            logger.info("Unregistered webhook: %s", webhook_id)
            return True
        return False

    def get_webhook(self, webhook_id: str) -> Optional[Webhook]:
        """Get a webhook by ID."""
        return self._webhooks.get(webhook_id)

    def list_webhooks(self, enabled_only: bool = False) -> List[Webhook]:
        """List all webhooks."""
        if enabled_only:
            return [w for w in self._webhooks.values() if w.enabled]
        return list(self._webhooks.values())

    async def trigger(
        self,
        webhook_id: str,
        event_data: Dict[str, Any],
    ) -> bool:
        """Trigger a webhook with event data.

        Returns True if webhook was triggered successfully.
        """
        webhook = self._webhooks.get(webhook_id)
        if not webhook or not webhook.enabled:
            return False

        # Check rate limit
        if not self._check_rate_limit(webhook_id, webhook.rate_limit):
            logger.warning("Webhook %s rate limited", webhook_id)
            webhook.last_error = "Rate limit exceeded"
            return False

        # Check event filter
        if webhook.event_filter and not self._matches_filter(event_data, webhook.event_filter):
            logger.debug("Webhook %s filter not matched", webhook_id)
            return False

        # Execute webhook
        success = await self._execute(webhook, event_data)

        # Update stats
        webhook.last_triggered = time.time()
        if success:
            webhook.success_count += 1
        else:
            webhook.failure_count += 1

        return success

    async def trigger_all(
        self,
        event_type: str,
        event_data: Dict[str, Any],
    ) -> Dict[str, bool]:
        """Trigger all matching webhooks for an event.

        Returns dict of webhook_id -> success status.
        """
        results = {}

        # Add event type to data
        event_data["_event_type"] = event_type

        for webhook_id, webhook in list(self._webhooks.items()):
            if not webhook.enabled:
                continue

            # Check if this webhook should be triggered for this event
            # (webhook name/id contains event type or no specific filter)
            if event_type.lower() in webhook.name.lower() or event_type.lower() in webhook.id.lower():
                results[webhook_id] = await self.trigger(webhook_id, event_data)

        return results

    def _check_rate_limit(self, webhook_id: str, limit: int) -> bool:
        """Check if webhook is within rate limit."""
        now = time.time()
        minute_ago = now - 60

        # Get or initialize rate limit history
        if webhook_id not in self._rate_limiters:
            self._rate_limiters[webhook_id] = []

        # Clean old entries
        self._rate_limiters[webhook_id] = [
            t for t in self._rate_limiters[webhook_id] if t > minute_ago
        ]

        # Check limit
        if len(self._rate_limiters[webhook_id]) >= limit:
            return False

        # Record this request
        self._rate_limiters[webhook_id].append(now)
        return True

    def _matches_filter(
        self, event_data: Dict[str, Any], filter_expr: str
    ) -> bool:
        """Check if event data matches a filter expression."""
        try:
            # Simple JSON-based filter
            # In production, use json-logic or similar
            if not filter_expr.strip():
                return True

            # Parse simple key=value pairs
            for condition in filter_expr.split(","):
                condition = condition.strip()
                if not condition:
                    continue

                if "=" in condition:
                    key, value = condition.split("=", 1)
                    key = key.strip()
                    value = value.strip()

                    # Check nested keys
                    keys = key.split(".")
                    data = event_data
                    for k in keys:
                        if isinstance(data, dict):
                            data = data.get(k)
                        else:
                            data = None

                    if data is None:
                        return False

                    # Simple string comparison
                    if str(data) != value:
                        return False

            return True
        except Exception as exc:
            logger.warning("Filter parse error: %s", exc)
            return True  # Fail open

    def _render_payload(
        self,
        template: str,
        event_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Render webhook payload template."""
        try:
            # Simple template rendering with {{key}} syntax
            rendered = template
            for key, value in self._flatten_dict(event_data).items():
                placeholder = f"{{{{{key}}}}}"
                if placeholder in rendered:
                    rendered = rendered.replace(placeholder, str(value))

            return json.loads(rendered)
        except Exception as exc:
            logger.warning("Template render error: %s", exc)
            return event_data

    def _flatten_dict(
        self, d: Dict[str, Any], parent_key: str = "", sep: str = "."
    ) -> Dict[str, Any]:
        """Flatten nested dict for template rendering."""
        items: List[Tuple[str, Any]] = []
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.extend(self._flatten_dict(v, new_key, sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    def _build_headers(self, webhook: Webhook) -> Dict[str, str]:
        """Build HTTP headers including auth."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "OneAgent-Webhook/1.0",
            "X-Webhook-ID": webhook.id,
        }

        if webhook.auth_type == WebhookAuth.API_KEY.value:
            headers["X-API-Key"] = webhook.api_key
        elif webhook.auth_type == WebhookAuth.BEARER.value:
            headers["Authorization"] = f"Bearer {webhook.api_key}"
        elif webhook.auth_type == WebhookAuth.HMAC_SHA256.value:
            # Signature will be computed per-request
            headers["X-Webhook-Signature"] = ""  # Set in _execute

        return headers

    def _compute_hmac_signature(
        self, payload: str, secret: str
    ) -> str:
        """Compute HMAC-SHA256 signature."""
        signature = hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"

    async def _execute(
        self,
        webhook: Webhook,
        event_data: Dict[str, Any],
    ) -> bool:
        """Execute a webhook with retry."""
        payload = self._render_payload(webhook.payload_template, event_data)
        payload_str = json.dumps(payload, ensure_ascii=False)
        headers = self._build_headers(webhook)

        # Compute HMAC signature if needed
        if webhook.auth_type == WebhookAuth.HMAC_SHA256.value:
            signature = self._compute_hmac_signature(payload_str, webhook.secret_key)
            headers["X-Webhook-Signature"] = signature

        for attempt in range(webhook.max_retries + 1):
            try:
                client = await self._get_client()

                async with self._semaphore:
                    response = await client.request(
                        method=webhook.method,
                        url=webhook.url,
                        headers=headers,
                        content=payload_str,
                    )

                if response.status_code < 400:
                    logger.info(
                        "Webhook %s triggered successfully (%d %s)",
                        webhook.id, response.status_code, response.reason_phrase
                    )
                    return True

                logger.warning(
                    "Webhook %s returned %d %s (attempt %d/%d)",
                    webhook.id, response.status_code, response.reason_phrase,
                    attempt + 1, webhook.max_retries + 1
                )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Webhook %s failed (attempt %d/%d): %s",
                    webhook.id, attempt + 1, webhook.max_retries + 1, exc
                )
                webhook.last_error = str(exc)[:200]

            # Retry with backoff
            if attempt < webhook.max_retries:
                delay = webhook.retry_delay * (2 ** attempt)
                await asyncio.sleep(delay)

        return False

    def get_stats(self) -> Dict[str, Any]:
        """Get webhook statistics."""
        webhooks = list(self._webhooks.values())
        return {
            "total_webhooks": len(webhooks),
            "enabled_webhooks": sum(1 for w in webhooks if w.enabled),
            "total_triggers": sum(w.success_count + w.failure_count for w in webhooks),
            "total_successes": sum(w.success_count for w in webhooks),
            "total_failures": sum(w.failure_count for w in webhooks),
            "webhooks": [
                {
                    "id": w.id,
                    "name": w.name,
                    "url": w.url,
                    "enabled": w.enabled,
                    "last_triggered": w.last_triggered,
                    "success_count": w.success_count,
                    "failure_count": w.failure_count,
                }
                for w in webhooks
            ],
        }


# Singleton
_webhook_trigger: Optional[WebhookTrigger] = None


def get_webhook_trigger() -> WebhookTrigger:
    """Get the shared webhook trigger instance."""
    global _webhook_trigger
    if _webhook_trigger is None:
        _webhook_trigger = WebhookTrigger()
    return _webhook_trigger


# ======================================================= Common webhook helpers

def create_slack_webhook(
    webhook_url: str,
    channel: str = "",
) -> Webhook:
    """Create a Slack-compatible webhook."""
    return Webhook(
        name=f"slack-{channel or 'default'}",
        url=webhook_url,
        method=WebhookMethod.POST.value,
        payload_template=json.dumps({
            "text": "{{content}}",
            "channel": channel,
        }),
    )


def create_generic_webhook(
    name: str,
    url: str,
    event_type: str,
    auth_type: str = WebhookAuth.NONE.value,
    api_key: str = "",
) -> Webhook:
    """Create a generic webhook."""
    return Webhook(
        name=name,
        url=url,
        event_filter=f"_event_type={event_type}",
        auth_type=auth_type,
        api_key=api_key,
    )