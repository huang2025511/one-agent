"""Common SQLite storage base class for memory modules.

Shared initialization, connection setup, WAL mode, and cleanup logic
used by :class:`SessionStore`, :class:`EmbeddingStore`, and
:class:`KnowledgeGraph`.

.. note::
    These stores are designed for **single-process** usage with asyncio.
    WAL mode + ``check_same_thread=False`` allows concurrent reads from
    multiple threads/coroutines, but simultaneous writes may cause
    "database is locked" errors. The retry logic in :meth:`_execute_with_retry`
    handles transient lock conflicts with exponential backoff.
    
    For multi-process deployments, consider using a connection pool with
    a write queue to serialize writes.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# Retry configuration for "database is locked" errors
SQLITE_RETRY_ATTEMPTS = 3
SQLITE_RETRY_BASE_DELAY = 0.01  # 10ms
SQLITE_RETRY_MAX_DELAY = 0.1   # 100ms


class BaseSQLiteStore:
    """Base class for SQLite-backed memory stores.

    Subclasses are responsible for creating their own schema by overriding
    :meth:`_init_db`, which is called after the connection has been
    established and WAL mode enabled.
    
    .. note::
        Designed for single-process asyncio usage. For multi-process scenarios,
        implement external write serialization (e.g., connection pool with queue).
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(
            db_path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Set busy timeout to wait up to 5 seconds for locks
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_db()

    def _init_db(self) -> None:  # pragma: no cover - abstract
        """Initialize schema. Override in subclasses."""
        raise NotImplementedError

    def _execute_with_retry(
        self,
        sql: str,
        params: Tuple[Any, ...] = (),
        operation: str = "execute",
    ) -> Optional[sqlite3.Row]:
        """Execute SQL with retry logic for "database is locked" errors.
        
        Uses exponential backoff: 10ms, 20ms, 40ms (capped at 100ms).
        
        Args:
            sql: SQL statement to execute
            params: Query parameters
            operation: "execute" for single row, "executemany" for bulk
            
        Returns:
            Last row for queries, None for DML statements
            
        Raises:
            sqlite3.OperationalError: If all retries exhausted
        """
        last_error = None
        for attempt in range(SQLITE_RETRY_ATTEMPTS):
            try:
                if operation == "executemany":
                    self._conn.executemany(sql, params)
                else:
                    cursor = self._conn.execute(sql, params)
                    # For SELECT queries, return the result
                    if sql.strip().upper().startswith("SELECT"):
                        return cursor.fetchone()
                    # For INSERT/UPDATE/DELETE, commit and return None
                    self._conn.commit()
                    return None
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" in str(exc).lower() and attempt < SQLITE_RETRY_ATTEMPTS - 1:
                    delay = min(
                        SQLITE_RETRY_BASE_DELAY * (2 ** attempt),
                        SQLITE_RETRY_MAX_DELAY
                    )
                    logger.warning(
                        "database locked (attempt %d/%d), retrying in %.0fms: %s",
                        attempt + 1, SQLITE_RETRY_ATTEMPTS, delay * 1000, exc
                    )
                    time.sleep(delay)
                    continue
                # Non-lock error or final attempt
                raise
        
        # All retries exhausted
        if last_error:
            raise last_error

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
