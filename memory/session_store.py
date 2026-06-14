"""Session persistence — store/retrieve chat sessions in SQLite."""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionStore:
    """Thread-safe SQLite-backed session store.

    Persists chat sessions and their messages so they survive restarts.
    Uses ``check_same_thread=False`` for cross-thread access from
    async event handlers.
    """

    def __init__(self, db_path: str = "data/memory/sessions.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                created_at REAL,
                updated_at REAL,
                message_count INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                meta TEXT DEFAULT '{}',
                created_at REAL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        """)
        self._conn.commit()

    # -------------------------------------------------------- public API

    def create_session(self, session_id: str, title: str = "") -> None:
        """Create a new session record (idempotent — upsert)."""
        now = time.time()
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions(id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, title, now, now),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("create_session(%s) failed: %s", session_id, exc)

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        meta: Optional[dict] = None,
        tokens: int = 0,
    ) -> None:
        """Append a message to a session and update counters.

        If the session does not exist yet, it is created automatically.
        The title is auto-generated from the first user message (first 80 chars).
        """
        now = time.time()
        meta_json = json.dumps(meta or {})

        try:
            # Ensure session exists (auto-create)
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions(id, created_at, updated_at) "
                "VALUES (?, ?, ?)",
                (session_id, now, now),
            )

            # Auto-title from first user message
            if role == "user":
                cur = self._conn.execute(
                    "SELECT title, message_count FROM sessions WHERE id = ?",
                    (session_id,),
                )
                row = cur.fetchone()
                if row and row["message_count"] == 0 and not row["title"]:
                    title = content[:80].replace("\n", " ").strip()
                    self._conn.execute(
                        "UPDATE sessions SET title = ? WHERE id = ?",
                        (title, session_id),
                    )

            # Insert message
            self._conn.execute(
                "INSERT INTO messages(session_id, role, content, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, meta_json, now),
            )

            # Update session counters
            self._conn.execute(
                "UPDATE sessions SET "
                "updated_at = ?, "
                "message_count = message_count + 1, "
                "total_tokens = total_tokens + ? "
                "WHERE id = ?",
                (now, tokens, session_id),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("add_message(%s) failed: %s", session_id, exc)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session with all its messages."""
        try:
            cur = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            session = dict(row)
            # Fetch messages
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            )
            messages = []
            for msg_row in cur.fetchall():
                msg = dict(msg_row)
                try:
                    msg["meta"] = json.loads(msg.get("meta", "{}"))
                except (json.JSONDecodeError, TypeError):
                    msg["meta"] = {}
                messages.append(msg)
            session["messages"] = messages
            return session
        except sqlite3.Error as exc:
            logger.error("get_session(%s) failed: %s", session_id, exc)
            return None

    def list_sessions(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """List recent sessions ordered by last update time."""
        try:
            cur = self._conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return [dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.error("list_sessions failed: %s", exc)
            return []

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages. Returns True if deleted."""
        try:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("delete_session(%s) failed: %s", session_id, exc)
            return False

    def get_session_count(self) -> int:
        """Return total number of sessions."""
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM sessions")
            return cur.fetchone()[0]
        except sqlite3.Error as exc:
            logger.error("get_session_count failed: %s", exc)
            return 0

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass