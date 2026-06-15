"""Common SQLite storage base class for memory modules.

Shared initialization, connection setup, WAL mode, and cleanup logic
used by :class:`SessionStore`, :class:`EmbeddingStore`, and
:class:`KnowledgeGraph`.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class BaseSQLiteStore:
    """Base class for SQLite-backed memory stores.

    Subclasses are responsible for creating their own schema by overriding
    :meth:`_init_db`, which is called after the connection has been
    established and WAL mode enabled.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(
            db_path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:  # pragma: no cover - abstract
        """Initialize schema. Override in subclasses."""
        raise NotImplementedError

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
