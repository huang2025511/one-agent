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
import threading
import time
from pathlib import Path
from typing import Any, Optional, Tuple

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

    Thread safety: a ``threading.Lock`` serializes all write operations
    to prevent "database is locked" errors and data corruption when
    multiple asyncio tasks (running via ``asyncio.to_thread``) access
    the same connection concurrently.

    Schema versioning (P2-1 fix): subclasses set ``SCHEMA_VERSION`` and
    implement :meth:`_migrate` for version-specific migrations. The base
    class reads ``PRAGMA user_version`` before ``_init_db`` and passes
    the old version so subclasses can run conditional migrations.
    """

    #: Current schema version. Subclasses override this.
    SCHEMA_VERSION: int = 1

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
        # Write lock — serializes all write operations across threads.
        # Uses RLock (reentrant) because some write methods call other
        # write methods (e.g. KnowledgeGraph.add_relation calls add_entity).
        self._write_lock = threading.RLock()
        # P2-1: read existing schema version before init
        cur = self._conn.execute("PRAGMA user_version")
        self._old_schema_version: int = cur.fetchone()[0]
        self._init_db()
        # P2-1: bump version after successful init/migration
        if self._old_schema_version < self.SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self._conn.commit()

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
        if getattr(self, "_closed", False):
            return
        try:
            if self._conn:
                self._conn.close()
                self._conn = None
        except Exception as exc:  # noqa: BLE001
            logger.debug("base_store close error: %s", exc)
        finally:
            self._closed = True

    def __del__(self):
        """Ensure connection is closed on garbage collection."""
        if getattr(self, "_closed", False):
            return
        if hasattr(self, "_conn") and self._conn:
            try:
                self._conn.close()
            except Exception as exc:
                logger.debug("base_store close on GC failed: %s", exc)
