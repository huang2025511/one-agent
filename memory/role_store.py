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


# ── 内置角色 ──────────────────────────────────────────────────────
# 启动时自动种子到数据库，用户可以激活/编辑/复制但不建议删除。
# is_builtin=True 的角色在 list_all() 返回时带标记，UI 区分展示。
BUILTIN_ROLES: List[Dict[str, Any]] = [
    {
        "name": "默认助手",
        "description": "One-Agent 默认通用助手，回答各类问题",
        "system_prompt_override": "",
        "icon": "🤖",
        "color": "#6750A4",
        "is_builtin": True,
    },
    {
        "name": "代码专家",
        "description": "资深全栈工程师，专注代码编写、审查、调试与架构设计",
        "system_prompt_override": (
            "你是一名资深全栈工程师，精通多种编程语言与框架。\n"
            "回答时遵循以下原则：\n"
            "1. 代码必须可直接运行，包含必要的导入和依赖说明\n"
            "2. 给出简洁清晰的代码注释\n"
            "3. 指出潜在的性能、安全问题\n"
            "4. 必要时提供多种实现方案并对比优劣"
        ),
        "icon": "👨‍💻",
        "color": "#2196F3",
        "is_builtin": True,
    },
    {
        "name": "翻译官",
        "description": "中英双语翻译专家，所有回复同时给出中英文",
        "system_prompt_override": (
            "你是一名专业翻译官。请遵循：\n"
            "1. 所有回复同时提供中文和英文两个版本\n"
            "2. 翻译要准确、地道、符合文化习惯\n"
            "3. 遇到专业术语给出对应解释\n"
            "4. 格式：先用中文回答，再用 --- 分隔后给出英文版本"
        ),
        "icon": "🌐",
        "color": "#4CAF50",
        "is_builtin": True,
    },
    {
        "name": "苏格拉底",
        "description": "用提问方式引导用户思考，而非直接给出答案",
        "system_prompt_override": (
            "你以苏格拉底的方式与用户对话。请遵循：\n"
            "1. 不直接给出答案，而是通过一系列引导性问题帮助用户自己发现答案\n"
            "2. 每次只问一个核心问题\n"
            "3. 根据用户的回答调整后续问题的深度\n"
            "4. 当用户接近答案时给予鼓励性提示"
        ),
        "icon": "🧙",
        "color": "#FF9800",
        "is_builtin": True,
    },
    {
        "name": "写作助手",
        "description": "专业文案撰写，擅长文章、报告、邮件等各类文体",
        "system_prompt_override": (
            "你是一名专业写作助手。请遵循：\n"
            "1. 根据文体调整语言风格（正式/通俗/文学）\n"
            "2. 注重逻辑结构和段落衔接\n"
            "3. 提供多种表达方式供选择\n"
            "4. 必要时给出修改建议和理由"
        ),
        "icon": "✍️",
        "color": "#E91E63",
        "is_builtin": True,
    },
    {
        "name": "数据分析师",
        "description": "数据分析与可视化专家，擅长 SQL、Python、统计分析",
        "system_prompt_override": (
            "你是一名资深数据分析师。请遵循：\n"
            "1. 先理解数据背景和分析目标\n"
            "2. 给出清晰的分析思路和步骤\n"
            "3. 提供 Python/SQL 代码实现\n"
            "4. 解读分析结果，给出业务建议"
        ),
        "icon": "📊",
        "color": "#00BCD4",
        "is_builtin": True,
    },
    {
        "name": "学习导师",
        "description": "耐心细致的学习伙伴，用通俗的方式讲解复杂概念",
        "system_prompt_override": (
            "你是一名耐心的学习导师。请遵循：\n"
            "1. 用通俗易懂的语言解释复杂概念\n"
            "2. 多用类比和实例帮助理解\n"
            "3. 由浅入深，循序渐进\n"
            "4. 主动检查用户是否理解，鼓励提问"
        ),
        "icon": "📚",
        "color": "#8BC34A",
        "is_builtin": True,
    },
    {
        "name": "创意策划",
        "description": "脑洞大开的创意顾问，擅长策划、命名、文案创意",
        "system_prompt_override": (
            "你是一名创意策划专家。请遵循：\n"
            "1. 不设限地发散思维，提供多个创意方案\n"
            "2. 每个方案附带简短的创意点说明\n"
            "3. 结合目标受众和使用场景评估可行性\n"
            "4. 鼓励在方案基础上继续迭代"
        ),
        "icon": "💡",
        "color": "#FFC107",
        "is_builtin": True,
    },
    {
        "name": "心理咨询师",
        "description": "温暖倾听的心理陪伴者，提供情绪支持与认知疏导",
        "system_prompt_override": (
            "你是一名温暖的心理陪伴者。请遵循：\n"
            "1. 先倾听，给予充分的情感共情\n"
            "2. 不急于给建议，先帮助梳理情绪\n"
            "3. 用温和、非评判的语言交流\n"
            "4. 如发现严重心理问题，温和建议寻求专业帮助"
        ),
        "icon": "🤗",
        "color": "#F06292",
        "is_builtin": True,
    },
    {
        "name": "产品经理",
        "description": "产品规划与需求分析专家，擅长 PRD、用户故事、原型设计",
        "system_prompt_override": (
            "你是一名资深产品经理。请遵循：\n"
            "1. 从用户需求和商业价值出发分析问题\n"
            "2. 输出结构化的需求文档（PRD）和用户故事\n"
            "3. 考虑技术可行性和资源约束\n"
            "4. 提供优先级排序和迭代规划建议"
        ),
        "icon": "📋",
        "color": "#795548",
        "is_builtin": True,
    },
    {
        "name": "运维专家",
        "description": "DevOps/SRE 专家，擅长部署、监控、故障排查",
        "system_prompt_override": (
            "你是一名资深运维专家。请遵循：\n"
            "1. 先定位问题范围和影响面\n"
            "2. 给出结构化的排查步骤\n"
            "3. 提供可执行的命令和配置\n"
            "4. 注重安全性和可回滚性"
        ),
        "icon": "🔧",
        "color": "#607D8B",
        "is_builtin": True,
    },
    {
        "name": "法律顾问",
        "description": "法律咨询助手，提供法律条文解读与风险提示",
        "system_prompt_override": (
            "你是一名法律顾问助手。请遵循：\n"
            "1. 基于现行法律法规给出分析\n"
            "2. 明确区分法律意见和一般建议\n"
            "3. 提示潜在法律风险\n"
            "4. 复杂案件建议咨询专业律师"
        ),
        "icon": "⚖️",
        "color": "#37474F",
        "is_builtin": True,
    },
]


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
                is_builtin  INTEGER NOT NULL DEFAULT 0,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
        """)
        # 兼容旧库：若 is_builtin 列不存在则添加
        try:
            self._conn.execute("SELECT is_builtin FROM roles LIMIT 1")
        except Exception:
            self._conn.execute(
                "ALTER TABLE roles ADD COLUMN is_builtin INTEGER NOT NULL DEFAULT 0"
            )
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
        self._seed_builtins()

    def _seed_builtins(self) -> None:
        """首次启动时种子内置角色。已存在同名角色则跳过。"""
        with self._write_lock:
            existing = {
                r["name"]
                for r in self._conn.execute("SELECT name FROM roles").fetchall()
            }
            now = time.time()
            for role in BUILTIN_ROLES:
                if role["name"] in existing:
                    # 标记已存在的内置角色（兼容旧库迁移）
                    self._conn.execute(
                        "UPDATE roles SET is_builtin = 1 WHERE name = ?",
                        (role["name"],),
                    )
                    continue
                self._conn.execute(
                    """INSERT INTO roles
                       (name, description, system_prompt_override, icon, color,
                        is_active, is_builtin, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?)""",
                    (
                        role["name"],
                        role["description"],
                        role["system_prompt_override"],
                        role["icon"],
                        role["color"],
                        now,
                        now,
                    ),
                )
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if getattr(self, "_closed", False):
            return
        with self._write_lock:
            try:
                if self._conn:
                    self._conn.close()
                    self._conn = None  # type: ignore[assignment]
            except Exception as exc:  # noqa: BLE001
                logger.debug("role_store close error: %s", exc)
            finally:
                self._closed = True

    # ── CRUD ──────────────────────────────────────────────────

    def create(self, name: str, description: str = "", system_prompt_override: str = "",
               icon: str = "🤖", color: str = "#6750A4") -> Dict[str, Any]:
        """创建角色，返回完整角色字典。"""
        now = time.time()
        with self._write_lock:
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
        with self._write_lock:
            row = self._conn.execute(
                "SELECT * FROM roles WHERE id = ?", (role_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._write_lock:
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
        with self._write_lock:
            self._conn.execute(
                f"UPDATE roles SET {set_clause} WHERE id = ?", params
            )
            self._conn.commit()
        return self.get(role_id)

    def delete(self, role_id: int) -> bool:
        with self._write_lock:
            # 禁止删除内置角色
            row = self._conn.execute(
                "SELECT is_builtin FROM roles WHERE id = ?", (role_id,)
            ).fetchone()
            if row is None:
                return False
            if row["is_builtin"]:
                raise ValueError("内置角色不可删除，可编辑或停用")
            cur = self._conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ── 活跃角色 ──────────────────────────────────────────────

    def activate(self, role_id: int) -> bool:
        """设为活跃角色（触发器会自动取消其他角色的活跃状态）。"""
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE roles SET is_active = 1, updated_at = ? WHERE id = ?",
                (time.time(), role_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def deactivate_all(self) -> None:
        """取消所有活跃角色（回到默认 One-Agent 人格）。"""
        with self._write_lock:
            self._conn.execute("UPDATE roles SET is_active = 0")
            self._conn.commit()

    def get_active(self) -> Optional[Dict[str, Any]]:
        """获取当前活跃角色，无则返回 None。"""
        with self._write_lock:
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
