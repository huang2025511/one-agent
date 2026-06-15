"""Session persistence — store/retrieve chat sessions in SQLite."""

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

from .base_store import BaseSQLiteStore

logger = logging.getLogger(__name__)


class SessionStore(BaseSQLiteStore):
    """Thread-safe SQLite-backed session store.

    Persists chat sessions and their messages so they survive restarts.
    Uses ``check_same_thread=False`` for cross-thread access from
    async event handlers.
    """

    def __init__(self, db_path: str = "data/memory/sessions.db") -> None:
        super().__init__(db_path)

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                created_at REAL,
                updated_at REAL,
                message_count INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                parent_id TEXT,
                fork_point INTEGER
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
        # Add fork columns to existing tables
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN parent_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN fork_point INTEGER")
        except sqlite3.OperationalError:
            pass  # Column already exists
        self._conn.commit()

    # -------------------------------------------------------- public API

    def create_session(self, session_id: str, title: str = "") -> None:
        """Create a new session record (idempotent — upsert)."""
        assert session_id, "session_id cannot be empty"
        assert isinstance(session_id, str), "session_id must be a string"
        
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
        message_id: Optional[str] = None,
    ) -> None:
        """Append a message to a session and update counters with idempotency support.

        If the session does not exist yet, it is created automatically.
        The title is auto-generated from the first user message (first 80 chars).
        
        Args:
            session_id: Session identifier
            role: Message role (user/assistant/system)
            content: Message content
            meta: Optional metadata dictionary
            tokens: Token count for this message
            message_id: Optional unique message ID for idempotency. If provided and
                       a message with this ID already exists, the operation is skipped.
        """
        assert session_id, "session_id cannot be empty"
        assert role in ("user", "assistant", "system"), "role must be user/assistant/system"
        assert isinstance(content, str), "content must be a string"
        assert tokens >= 0, "tokens must be non-negative"
        
        now = time.time()
        meta_json = json.dumps(meta or {})

        try:
            # Idempotency check: if message_id provided, check if it already exists
            if message_id:
                cur = self._conn.execute(
                    "SELECT id FROM messages WHERE id = ?", (message_id,)
                )
                if cur.fetchone():
                    return  # Message already exists, skip insertion
            
            with self._conn:  # automatic transaction
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

                # Insert message with optional ID
                if message_id:
                    self._conn.execute(
                        "INSERT INTO messages(id, session_id, role, content, meta, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (message_id, session_id, role, content, meta_json, now),
                    )
                else:
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
        except sqlite3.Error as exc:
            logger.error("add_message(%s) transaction failed: %s", session_id, exc)
            raise

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session with all its messages."""
        assert session_id, "session_id cannot be empty"
        assert isinstance(session_id, str), "session_id must be a string"
        
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
        assert limit > 0, "limit must be positive"
        assert offset >= 0, "offset must be non-negative"
        
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
        assert session_id, "session_id cannot be empty"
        assert isinstance(session_id, str), "session_id must be a string"
        
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

    def fork_session(self, session_id: str, fork_point: int, new_session_id: Optional[str] = None) -> Optional[str]:
        """从指定消息位置分叉会话，创建新的对话分支。

        Args:
            session_id: 原始会话 ID
            fork_point: 分叉点（消息索引，从 0 开始）
            new_session_id: 新会话 ID（可选，不传则自动生成）

        Returns:
            新会话 ID，失败返回 None

        示例:
            # 从第 5 条消息处分叉
            new_id = store.fork_session("abc123", 5)
            # 新会话包含原会话的前 5 条消息
        """
        assert session_id, "session_id cannot be empty"
        assert isinstance(session_id, str), "session_id must be a string"
        assert fork_point >= 0, "fork_point must be non-negative"
        
        import uuid

        try:
            # 获取原始会话
            original = self.get_session(session_id)
            if not original:
                logger.error("fork_session: session %s not found", session_id)
                return None

            messages = original.get("messages", [])
            if fork_point < 0 or fork_point > len(messages):
                logger.error("fork_session: invalid fork_point %d (max %d)", fork_point, len(messages))
                return None

            # 生成新会话 ID
            if not new_session_id:
                new_session_id = f"fork_{session_id}_{uuid.uuid4().hex[:8]}"

            # 创建新会话
            now = time.time()
            title = f"[分支] {original.get('title', '')}"
            self._conn.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at, message_count, total_tokens, parent_id, fork_point) "
                "VALUES (?, ?, ?, ?, 0, 0, ?, ?)",
                (new_session_id, title, now, now, session_id, fork_point),
            )

            # 复制分叉点之前的消息
            for msg in messages[:fork_point]:
                meta = msg.get("meta", {})
                meta_json = json.dumps(meta) if isinstance(meta, dict) else (meta or "{}")
                self._conn.execute(
                    "INSERT INTO messages(id, session_id, role, content, meta, created_at) "
                    "VALUES (NULL, ?, ?, ?, ?, ?)",
                    (new_session_id, msg["role"], msg["content"], meta_json, msg["created_at"]),
                )

            # 更新消息计数和 token 统计
            self._conn.execute(
                "UPDATE sessions SET message_count = ?, total_tokens = ? WHERE id = ?",
                (fork_point, sum(m.get("tokens", 0) for m in messages[:fork_point]), new_session_id),
            )

            self._conn.commit()
            logger.info("fork_session: created %s from %s at point %d", new_session_id, session_id, fork_point)
            return new_session_id

        except sqlite3.Error as exc:
            logger.error("fork_session(%s) failed: %s", session_id, exc)
            self._conn.rollback()
            return None

    def get_session_tree(self, session_id: str) -> Dict[str, Any]:
        """获取会话的分支树结构。

        Args:
            session_id: 会话 ID

        Returns:
            包含 parent_id, children, fork_point 的树结构
        """
        assert session_id, "session_id cannot be empty"
        assert isinstance(session_id, str), "session_id must be a string"
        
        try:
            # 获取当前会话
            cur = self._conn.execute(
                "SELECT id, title, parent_id, fork_point FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return {}

            tree = {
                "id": row["id"],
                "title": row["title"],
                "parent_id": row["parent_id"],
                "fork_point": row["fork_point"],
                "children": [],
            }

            # 查找子会话
            cur = self._conn.execute(
                "SELECT id, title, fork_point FROM sessions WHERE parent_id = ?",
                (session_id,),
            )
            for child_row in cur.fetchall():
                tree["children"].append({
                    "id": child_row["id"],
                    "title": child_row["title"],
                    "fork_point": child_row["fork_point"],
                })

            return tree

        except sqlite3.Error as exc:
            logger.error("get_session_tree(%s) failed: %s", session_id, exc)
            return {}

    # -------------------------------------------------------- async wrappers

    async def create_session_async(self, session_id: str, title: str = "") -> None:
        """Async wrapper for create_session"""
        await asyncio.to_thread(self.create_session, session_id, title)

    async def add_message_async(
        self,
        session_id: str,
        role: str,
        content: str,
        meta: Optional[dict] = None,
        tokens: int = 0,
    ) -> None:
        """Async wrapper for add_message"""
        await asyncio.to_thread(self.add_message, session_id, role, content, meta, tokens)

    async def get_session_async(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Async wrapper for get_session"""
        return await asyncio.to_thread(self.get_session, session_id)

    async def list_sessions_async(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Async wrapper for list_sessions"""
        return await asyncio.to_thread(self.list_sessions, limit, offset)

    async def delete_session_async(self, session_id: str) -> bool:
        """Async wrapper for delete_session"""
        return await asyncio.to_thread(self.delete_session, session_id)

    async def get_session_count_async(self) -> int:
        """Async wrapper for get_session_count"""
        return await asyncio.to_thread(self.get_session_count)

    async def fork_session_async(
        self, session_id: str, fork_point: int, new_session_id: Optional[str] = None
    ) -> Optional[str]:
        """Async wrapper for fork_session"""
        return await asyncio.to_thread(self.fork_session, session_id, fork_point, new_session_id)

    async def get_session_tree_async(self, session_id: str) -> Dict[str, Any]:
        """Async wrapper for get_session_tree"""
        return await asyncio.to_thread(self.get_session_tree, session_id)