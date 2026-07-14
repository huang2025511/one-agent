"""Role Store — 用户自定义角色（system prompt 覆盖）的持久化存储。

角色允许用户自定义 Agent 的人格和行为模式，例如：
  - "翻译官"：所有回复中英双语
  - "代码审查员"：以严格标准审查代码
  - "苏格拉底"：用提问方式引导思考

角色存储在 SQLite 中，同一时刻只有一个活跃角色。
活跃角色的 system_prompt_override 会被注入到 LLM 的 system 消息开头。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .base_store import BaseSQLiteStore

logger = logging.getLogger(__name__)


class RoleStore(BaseSQLiteStore):
    """SQLite-backed role storage with CRUD + active role tracking."""

    def __init__(self, path: str) -> None:
        super().__init__(path)

    def _init_db(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS roles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                system_prompt_override TEXT NOT NULL DEFAULT '',
                icon        TEXT NOT NULL DEFAULT '🤖',
                color       TEXT NOT NULL DEFAULT '#6750A4',
                is_active   INTEGER NOT NULL DEFAULT 0,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
        """)
        # 确保同一时刻只有一个活跃角色
        self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS only_one_active_role
            AFTER UPDATE OF is_active ON roles
            WHEN NEW.is_active = 1
            BEGIN
                UPDATE roles SET is_active = 0 WHERE id != NEW.id;
            END
        """)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]

    # ── CRUD ──────────────────────────────────────────────────

    def create(self, name: str, description: str = "", system_prompt_override: str = "",
               icon: str = "🤖", color: str = "#6750A4") -> Dict[str, Any]:
        """创建角色，返回完整角色字典。"""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO roles (name, description, system_prompt_override, icon, color,
                                      is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (name, description, system_prompt_override, icon, color, now, now),
            )
            self._conn.commit()
            role_id = cur.lastrowid
        return self.get(role_id)  # type: ignore[return-value]

    def get(self, role_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM roles WHERE id = ?", (role_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM roles ORDER BY is_active DESC, updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update(self, role_id: int, **fields) -> Optional[Dict[str, Any]]:
        """更新角色字段，仅更新提供的字段。"""
        allowed = {"name", "description", "system_prompt_override", "icon", "color"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get(role_id)
        updates["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [role_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE roles SET {set_clause} WHERE id = ?", params
            )
            self._conn.commit()
        return self.get(role_id)

    def delete(self, role_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ── 活跃角色 ──────────────────────────────────────────────

    def activate(self, role_id: int) -> bool:
        """设为活跃角色（触发器会自动取消其他角色的活跃状态）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE roles SET is_active = 1, updated_at = ? WHERE id = ?",
                (time.time(), role_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def deactivate_all(self) -> None:
        """取消所有活跃角色（回到默认 One-Agent 人格）。"""
        with self._lock:
            self._conn.execute("UPDATE roles SET is_active = 0")
            self._conn.commit()

    def get_active(self) -> Optional[Dict[str, Any]]:
        """获取当前活跃角色，无则返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM roles WHERE is_active = 1 LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_active_prompt(self) -> str:
        """获取活跃角色的 system_prompt_override，无活跃角色返回空字符串。"""
        role = self.get_active()
        if role is None:
            return ""
        return role.get("system_prompt_override", "") or ""
