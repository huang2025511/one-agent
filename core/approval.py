"""Human-in-the-loop approval system for dangerous operations."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
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
        # 修复：添加 status 字段，客户端 approval.dart 的 fromApi 期望读取此字段
        return {
            "id": self.id,
            "operation": self.operation,
            "details": self.details,
            "source": self.source,
            "risk_level": self.risk_level,
            "created_at": self.created_at,
            "status": "pending" if self._approved is None else ("approved" if self._approved else "denied"),
        }


class ApprovalManager:
    """Manages pending approval requests.

    v1.0.110：可选 SQLite 持久化（db_path）。待办与历史落库，重启后
    待办不丢（TTL 内的 pending 会恢复，过期自动置 deny）。DB 任何
    故障都静默降级为纯内存模式，不影响审批功能本身。
    """

    _MAX_HISTORY = 1000
    # 待审批 TTL：超过该时长未处理的请求自动过期清理。
    # 之前 wait() 超时后请求仍永久留在 _pending 里（幽灵审批），
    # 既泄漏内存又让 UI 待办列表越积越长。
    _TTL_SECONDS = 300.0

    def __init__(self, db_path: Optional[str] = None):
        self._pending: Dict[str, ApprovalRequest] = {}
        self._history: list = []
        self._on_approval_needed: Optional[Callable] = None
        self._lock = threading.Lock()
        self._conn = None
        if db_path:
            try:
                from core.db import create_sqlite_connection
                self._conn = create_sqlite_connection(db_path)
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS approvals (
                        id         TEXT PRIMARY KEY,
                        operation  TEXT,
                        details    TEXT,
                        source     TEXT,
                        risk_level TEXT,
                        created_at REAL NOT NULL,
                        status     TEXT NOT NULL,   -- pending/approved/denied
                        decided_at REAL
                    )"""
                )
                self._conn.commit()
                self._load_from_db()
            except Exception as exc:
                logger.warning("approval persistence disabled (%s): %s",
                               db_path, exc)
                self._conn = None

    # -- DB helpers（全部容错：失败仅记日志，不阻断审批流） --------------

    def _db_exec(self, sql: str, params: tuple = ()) -> None:
        if self._conn is None:
            return
        try:
            with self._conn:
                self._conn.execute(sql, params)
        except Exception as exc:
            logger.debug("approval db write failed: %s", exc)

    def _load_from_db(self) -> None:
        """启动恢复：TTL 内的 pending 重建为可等待对象，过期的置 deny。"""
        if self._conn is None:
            return
        try:
            now = time.time()
            rows = self._conn.execute(
                "SELECT * FROM approvals ORDER BY created_at DESC").fetchall()
        except Exception as exc:
            logger.warning("approval db load failed: %s", exc)
            return
        for row in rows:
            if row["status"] == "pending":
                if now - row["created_at"] > self._TTL_SECONDS:
                    # 上次运行遗留的过期待办：置 deny 并入历史
                    self._db_exec(
                        "UPDATE approvals SET status='denied', decided_at=? "
                        "WHERE id=? AND status='pending'",
                        (now, row["id"]))
                    self._history.append(
                        {"id": row["id"], "approved": False, "time": now})
                    continue
                req = ApprovalRequest(
                    row["operation"], row["details"],
                    source=row["source"] or "agent",
                    risk_level=row["risk_level"] or "medium",
                )
                req.id = row["id"]
                req.created_at = row["created_at"]
                self._pending[req.id] = req
            else:
                self._history.append({
                    "id": row["id"],
                    "approved": row["status"] == "approved",
                    "time": row["decided_at"] or row["created_at"],
                })
        self._history = self._history[-self._MAX_HISTORY:]

    def _purge_expired(self) -> None:
        """调用方需持有 self._lock。过期请求置为 deny 以唤醒残留等待者。"""
        now = time.time()
        expired = [rid for rid, r in self._pending.items()
                   if now - r.created_at > self._TTL_SECONDS]
        for rid in expired:
            r = self._pending.pop(rid)
            if r._approved is None:
                r.deny()
                self._db_exec(
                    "UPDATE approvals SET status='denied', decided_at=? "
                    "WHERE id=? AND status='pending'", (now, rid))

    def request_approval(self, operation: str, details: str,
                        risk_level: str = "medium") -> ApprovalRequest:
        """Create a new approval request."""
        req = ApprovalRequest(operation, details, risk_level=risk_level)
        with self._lock:
            self._purge_expired()
            self._pending[req.id] = req
            self._db_exec(
                "INSERT OR REPLACE INTO approvals"
                "(id, operation, details, source, risk_level, created_at,"
                " status, decided_at) VALUES (?,?,?,?,?,?, 'pending', NULL)",
                (req.id, operation, details, req.source, req.risk_level,
                 req.created_at))

        if self._on_approval_needed:
            self._on_approval_needed(req)

        return req

    def get_pending(self) -> list:
        """List all pending requests."""
        with self._lock:
            self._purge_expired()
            return [r.to_dict() for r in self._pending.values()]

    def approve(self, request_id: str) -> bool:
        """Approve a pending request."""
        with self._lock:
            req = self._pending.pop(request_id, None)
            if req:
                req.approve()
                now = time.time()
                self._history.append({"id": request_id, "approved": True, "time": now})
                if len(self._history) > self._MAX_HISTORY:
                    self._history = self._history[-self._MAX_HISTORY:]
                self._db_exec(
                    "UPDATE approvals SET status='approved', decided_at=? WHERE id=?",
                    (now, request_id))
                return True
        return False

    def deny(self, request_id: str) -> bool:
        """Deny a pending request."""
        with self._lock:
            req = self._pending.pop(request_id, None)
            if req:
                req.deny()
                now = time.time()
                self._history.append({"id": request_id, "approved": False, "time": now})
                if len(self._history) > self._MAX_HISTORY:
                    self._history = self._history[-self._MAX_HISTORY:]
                self._db_exec(
                    "UPDATE approvals SET status='denied', decided_at=? WHERE id=?",
                    (now, request_id))
                return True
        return False
