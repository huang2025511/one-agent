"""Human-in-the-loop approval system for dangerous operations."""

from __future__ import annotations

import asyncio
import time
import uuid
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ApprovalRequest:
    """A pending approval request."""
    def __init__(self, operation: str, details: str, source: str = "agent",
                 risk_level: str = "medium"):
        self.id = uuid.uuid4().hex[:12]
        self.operation = operation
        self.details = details
        self.source = source
        self.risk_level = risk_level  # low, medium, high, critical
        self.created_at = time.time()
        self._event = asyncio.Event()
        self._approved: Optional[bool] = None

    def approve(self) -> None:
        self._approved = True
        self._event.set()

    def deny(self) -> None:
        self._approved = False
        self._event.set()

    async def wait(self, timeout: float = 120.0) -> bool:
        """Wait for approval. Returns True if approved, False otherwise."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
            return self._approved is True
        except asyncio.TimeoutError:
            return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "operation": self.operation,
            "details": self.details,
            "source": self.source,
            "risk_level": self.risk_level,
            "created_at": self.created_at,
        }


class ApprovalManager:
    """Manages pending approval requests."""

    def __init__(self):
        self._pending: Dict[str, ApprovalRequest] = {}
        self._history: list = []
        self._on_approval_needed: Optional[Callable] = None

    def request_approval(self, operation: str, details: str,
                        risk_level: str = "medium") -> ApprovalRequest:
        """Create a new approval request."""
        req = ApprovalRequest(operation, details, risk_level=risk_level)
        self._pending[req.id] = req

        if self._on_approval_needed:
            self._on_approval_needed(req)

        return req

    def get_pending(self) -> list:
        """List all pending requests."""
        return [r.to_dict() for r in self._pending.values()]

    def approve(self, request_id: str) -> bool:
        """Approve a pending request."""
        req = self._pending.pop(request_id, None)
        if req:
            req.approve()
            self._history.append({"id": request_id, "approved": True, "time": time.time()})
            return True
        return False

    def deny(self, request_id: str) -> bool:
        """Deny a pending request."""
        req = self._pending.pop(request_id, None)
        if req:
            req.deny()
            self._history.append({"id": request_id, "approved": False, "time": time.time()})
            return True
        return False

    def set_callback(self, cb: Callable) -> None:
        """Set callback for when approval is needed (e.g., WebSocket notification)."""
        self._on_approval_needed = cb