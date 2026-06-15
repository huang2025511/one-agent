"""Audit log — persistent operation tracking for security and debugging.

Records all significant operations (API calls, skill executions, config changes,
sensitive actions) to a SQLite database with automatic rotation.

Thread-safe: uses check_same_thread=False for cross-thread async access.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Audit log configuration
AUDIT_LOG_PATH = "data/memory/audit.db"
AUDIT_RETENTION_DAYS = 30
AUDIT_MAX_ENTRIES = 100000  # Auto-rotate when exceeded


class AuditLog:
    """Persistent audit log for tracking system operations.
    
    Records:
    - API endpoint calls (who/when/what)
    - Skill executions
    - Configuration changes
    - Authentication events
    - Sensitive operations (marketplace publish, etc.)
    
    Provides query API for dashboards and compliance.
    """

    def __init__(self, db_path: str = AUDIT_LOG_PATH) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        """Create audit log table if not exists."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                actor TEXT,
                action TEXT NOT NULL,
                resource TEXT,
                details TEXT,
                ip_address TEXT,
                status TEXT DEFAULT 'success',
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
            CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
        """)
        self._conn.commit()

    def log(
        self,
        action: str,
        actor: Optional[str] = None,
        resource: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        status: str = "success",
    ) -> None:
        """Record an audit event.
        
        Args:
            action: What happened (e.g., "api_call", "skill_execute", "config_change")
            actor: Who did it (e.g., user ID, API key hash, "system")
            resource: What was affected (e.g., endpoint path, skill ID)
            details: Additional context (JSON-serializable dict)
            ip_address: Client IP address
            status: "success" or "failure"
        """
        try:
            details_json = json.dumps(details) if details else None
            self._conn.execute(
                """INSERT INTO audit_log 
                   (timestamp, actor, action, resource, details, ip_address, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), actor, action, resource, details_json, ip_address, status),
            )
            self._conn.commit()
            
            # Auto-rotate if too many entries
            self._check_rotation()
        except sqlite3.Error as exc:
            logger.error("Failed to write audit log: %s", exc)

    def _check_rotation(self) -> None:
        """Delete old entries if table exceeds max size."""
        try:
            count = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            if count > AUDIT_MAX_ENTRIES:
                # Keep only the most recent 80% of entries
                keep_count = int(AUDIT_MAX_ENTRIES * 0.8)
                self._conn.execute(
                    """DELETE FROM audit_log 
                       WHERE id NOT IN (
                           SELECT id FROM audit_log 
                           ORDER BY timestamp DESC 
                           LIMIT ?
                       )""",
                    (keep_count,),
                )
                self._conn.commit()
                logger.info("Audit log rotated: deleted %d old entries", count - keep_count)
        except sqlite3.Error as exc:
            logger.error("Audit log rotation failed: %s", exc)

    def query(
        self,
        action: Optional[str] = None,
        actor: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query audit log with filters.
        
        Args:
            action: Filter by action type
            actor: Filter by actor
            start_time: Filter events after this timestamp
            end_time: Filter events before this timestamp
            limit: Maximum number of results
            
        Returns:
            List of audit log entries
        """
        query_parts = ["SELECT * FROM audit_log WHERE 1=1"]
        params = []
        
        if action:
            query_parts.append("AND action = ?")
            params.append(action)
        if actor:
            query_parts.append("AND actor = ?")
            params.append(actor)
        if start_time:
            query_parts.append("AND timestamp >= ?")
            params.append(start_time)
        if end_time:
            query_parts.append("AND timestamp <= ?")
            params.append(end_time)
        
        query_parts.append("ORDER BY timestamp DESC LIMIT ?")
        params.append(limit)
        
        sql = " ".join(query_parts)
        
        try:
            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "resource": row["resource"],
                    "details": json.loads(row["details"]) if row["details"] else None,
                    "ip_address": row["ip_address"],
                    "status": row["status"],
                }
                for row in rows
            ]
        except sqlite3.Error as exc:
            logger.error("Audit log query failed: %s", exc)
            return []

    def stats(self) -> Dict[str, Any]:
        """Get audit log statistics."""
        try:
            total = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            
            # Count by action type
            action_counts = {}
            cursor = self._conn.execute(
                "SELECT action, COUNT(*) as count FROM audit_log GROUP BY action ORDER BY count DESC LIMIT 10"
            )
            for row in cursor.fetchall():
                action_counts[row["action"]] = row["count"]
            
            # Count by status
            status_counts = {}
            cursor = self._conn.execute(
                "SELECT status, COUNT(*) as count FROM audit_log GROUP BY status"
            )
            for row in cursor.fetchall():
                status_counts[row["status"]] = row["count"]
            
            # Oldest and newest entry
            oldest = self._conn.execute("SELECT MIN(timestamp) FROM audit_log").fetchone()[0]
            newest = self._conn.execute("SELECT MAX(timestamp) FROM audit_log").fetchone()[0]
            
            return {
                "total_entries": total,
                "action_counts": action_counts,
                "status_counts": status_counts,
                "oldest_entry": oldest,
                "newest_entry": newest,
                "retention_days": AUDIT_RETENTION_DAYS,
            }
        except sqlite3.Error as exc:
            logger.error("Audit log stats failed: %s", exc)
            return {"total_entries": 0, "error": str(exc)}

    def close(self) -> None:
        """Close the database connection."""
        try:
            if self._conn:
                self._conn.close()
                self._conn = None
        except Exception:
            pass

    def __del__(self):
        """Ensure connection is closed on garbage collection."""
        if hasattr(self, "_conn") and self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
