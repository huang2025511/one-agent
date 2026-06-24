"""SQLite connection factory and shared DB helpers.

Consolidates the ``sqlite3.connect`` + ``PRAGMA`` setup that was
duplicated across ``core/audit_log.py``, ``core/self_improve.py``,
``models/cost_tracker.py``, and ``skills/document_search.py``. Each of
these modules previously hand-rolled the same boilerplate
(``check_same_thread=False`` + WAL + ``busy_timeout``), but with
inconsistent PRAGMAs — some forgot WAL, some forgot ``busy_timeout``.
The factory here applies the full production-safe set uniformly.

``memory/base_store.py``'s :class:`BaseSQLiteStore` remains the
preferred base class for memory-module stores; this factory is for
modules that need a plain connection without the base-class lifecycle
(schema init, ``__del__``, retry logic).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


def create_sqlite_connection(
    db_path: str,
    *,
    isolation_level: Optional[str] = None,
    apply_wal: bool = True,
    busy_timeout_ms: int = 5000,
) -> sqlite3.Connection:
    """Create a production-safe SQLite connection.

    Args:
        db_path: Path to the SQLite database file. Parent directories
            are created automatically.
        isolation_level: Forwarded to :func:`sqlite3.connect`. ``None``
            means default (deferred) isolation; pass ``None`` explicitly
            for autocommit-mode stores (e.g. ``LongTermMemory``).
        apply_wal: Whether to enable WAL journal mode. WAL allows
            concurrent readers during writes and is the right default
            for all multi-threaded async stores.
        busy_timeout_ms: ``PRAGMA busy_timeout`` value in milliseconds.
            Causes ``OperationalError: database is locked`` to wait up
            to this long before failing, smoothing transient lock
            conflicts.

    Returns:
        A configured :class:`sqlite3.Connection` with
        ``row_factory = sqlite3.Row`` and ``check_same_thread=False``
        (the caller is responsible for serializing writes with a lock).
    """
    # Ensure parent directory exists
    parent = Path(db_path).parent
    if str(parent) and not parent.exists():
        os.makedirs(str(parent), exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    if apply_wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # WAL may be unavailable on some filesystems (e.g. network
            # mounts); fall back silently rather than crashing the store.
            pass
    if busy_timeout_ms > 0:
        try:
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        except sqlite3.DatabaseError:
            pass
    return conn
