"""Offline mode — queue operations when network is unavailable.

Provides a persistent queue for LLM requests that fail due to network issues.
Automatically retries when connectivity is restored.

Thread-safe: uses check_same_thread=False for cross-thread async access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Offline queue configuration
OFFLINE_QUEUE_PATH = "data/memory/offline_queue.db"
MAX_QUEUE_SIZE = 1000
RETRY_INTERVAL_SECONDS = 60


class OfflineQueue:
    """Persistent queue for offline operation retry.
    
    When LLM calls fail due to network issues, requests are queued here.
    A background task periodically retries queued requests.
    """

    def __init__(self, db_path: str = OFFLINE_QUEUE_PATH) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Serialize writes (see audit_log.py for rationale).
        self._write_lock = threading.Lock()
        self._init_schema()
        self._retry_task: Optional[asyncio.Task] = None

    def _init_schema(self) -> None:
        """Create offline queue table if not exists."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS offline_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                request_type TEXT NOT NULL,
                request_data TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                last_error TEXT,
                status TEXT DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS idx_queue_status ON offline_queue(status);
            CREATE INDEX IF NOT EXISTS idx_queue_created ON offline_queue(created_at);
        """)
        self._conn.commit()

    def enqueue(
        self,
        request_type: str,
        request_data: Dict[str, Any],
    ) -> int:
        """Add a request to the offline queue.
        
        Args:
            request_type: Type of request (e.g., "llm_call", "skill_execute")
            request_data: Request payload (JSON-serializable)
            
        Returns:
            Queue entry ID
        """
        try:
            with self._write_lock:
                # Check queue size limit
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM offline_queue WHERE status = 'pending'"
                ).fetchone()[0]
                if count >= MAX_QUEUE_SIZE:
                    logger.warning("Offline queue full (%d/%d), dropping oldest", count, MAX_QUEUE_SIZE)
                    self._conn.execute(
                        "DELETE FROM offline_queue WHERE id IN (SELECT id FROM offline_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1)"
                    )

                cursor = self._conn.execute(
                    """INSERT INTO offline_queue (created_at, request_type, request_data)
                       VALUES (?, ?, ?)""",
                    (time.time(), request_type, json.dumps(request_data)),
                )
                self._conn.commit()
            logger.info("Enqueued offline request: type=%s id=%d", request_type, cursor.lastrowid)
            return cursor.lastrowid
        except sqlite3.Error as exc:
            logger.error("Failed to enqueue offline request: %s", exc)
            return -1

    def dequeue(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get pending requests from the queue.
        
        Args:
            limit: Maximum number of requests to return
            
        Returns:
            List of queue entries
        """
        try:
            cursor = self._conn.execute(
                """SELECT * FROM offline_queue 
                   WHERE status = 'pending' 
                   ORDER BY created_at ASC 
                   LIMIT ?""",
                (limit,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "request_type": row["request_type"],
                    "request_data": json.loads(row["request_data"]),
                    "retry_count": row["retry_count"],
                    "last_error": row["last_error"],
                }
                for row in rows
            ]
        except sqlite3.Error as exc:
            logger.error("Failed to dequeue offline requests: %s", exc)
            return []

    def mark_success(self, queue_id: int) -> None:
        """Mark a queue entry as successfully processed."""
        try:
            with self._write_lock:
                self._conn.execute(
                    "UPDATE offline_queue SET status = 'completed' WHERE id = ?",
                    (queue_id,),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to mark queue entry success: %s", exc)

    def mark_failure(self, queue_id: int, error: str) -> None:
        """Mark a queue entry as failed and increment retry count."""
        try:
            with self._write_lock:
                self._conn.execute(
                    """UPDATE offline_queue
                       SET retry_count = retry_count + 1, last_error = ?
                       WHERE id = ?""",
                    (error, queue_id),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to mark queue entry failure: %s", exc)

    def stats(self) -> Dict[str, Any]:
        """Get offline queue statistics."""
        try:
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM offline_queue WHERE status = 'pending'"
            ).fetchone()[0]
            
            completed = self._conn.execute(
                "SELECT COUNT(*) FROM offline_queue WHERE status = 'completed'"
            ).fetchone()[0]
            
            failed = self._conn.execute(
                "SELECT COUNT(*) FROM offline_queue WHERE retry_count >= 3"
            ).fetchone()[0]
            
            oldest = self._conn.execute(
                "SELECT MIN(created_at) FROM offline_queue WHERE status = 'pending'"
            ).fetchone()[0]
            
            return {
                "pending": pending,
                "completed": completed,
                "failed": failed,
                "oldest_pending": oldest,
                "max_queue_size": MAX_QUEUE_SIZE,
            }
        except sqlite3.Error as exc:
            logger.error("Failed to get queue stats: %s", exc)
            return {"pending": 0, "completed": 0, "failed": 0}

    async def start_retry_loop(self, callback) -> None:
        """Start background task to retry queued requests.
        
        Args:
            callback: Async function to process each request. 
                     Signature: async def callback(request_type, request_data) -> bool
        """
        if self._retry_task and not self._retry_task.done():
            logger.warning("Offline retry loop already running")
            return
        
        async def _retry_loop():
            while True:
                try:
                    pending = self.dequeue(limit=5)
                    for entry in pending:
                        try:
                            success = await callback(
                                entry["request_type"],
                                entry["request_data"],
                            )
                            if success:
                                self.mark_success(entry["id"])
                                logger.info("Offline request %d succeeded", entry["id"])
                            else:
                                self.mark_failure(entry["id"], "Callback returned False")
                                logger.warning("Offline request %d failed", entry["id"])
                        except Exception as exc:
                            self.mark_failure(entry["id"], str(exc))
                            logger.error("Offline request %d error: %s", entry["id"], exc)
                    
                    await asyncio.sleep(RETRY_INTERVAL_SECONDS)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("Offline retry loop error: %s", exc)
                    await asyncio.sleep(RETRY_INTERVAL_SECONDS)
        
        self._retry_task = asyncio.create_task(_retry_loop())
        logger.info("Offline retry loop started")

    async def stop_retry_loop(self) -> None:
        """Stop the background retry task."""
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            logger.info("Offline retry loop stopped")

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
