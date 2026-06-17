"""SQLite connection pool manager."""

import sqlite3
import threading
from contextlib import contextmanager
from typing import List


class SQLiteConnectionPool:
    """Simple connection pool for SQLite databases."""

    def __init__(self, db_path: str, pool_size: int = 5):
        """Initialize connection pool.

        Args:
            db_path: Path to SQLite database file
            pool_size: Number of connections to maintain in pool
        """
        self.db_path = db_path
        self.pool_size = pool_size
        self._connections: List[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._init_pool()

    def _init_pool(self):
        """Create initial pool of connections."""
        for _ in range(self.pool_size):
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._connections.append(conn)

    @contextmanager
    def get_connection(self):
        """Get a connection from the pool.

        Yields:
            sqlite3.Connection from the pool

        Raises:
            RuntimeError: If no connections available
        """
        with self._lock:
            if not self._connections:
                raise RuntimeError("No connections available in pool")
            conn = self._connections.pop()

        try:
            yield conn
        finally:
            with self._lock:
                self._connections.append(conn)

    def close(self) -> None:
        """Close all connections in the pool (alias for close_all)."""
        self.close_all()

    def close_all(self):
        """Close all connections in the pool."""
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()

    def __del__(self):
        """Ensure connections are closed on garbage collection."""
        try:
            self.close()
        except Exception:
            pass
