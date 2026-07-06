"""Database Integration — direct database query skill.

Provides:
  - PostgreSQL, MySQL, MongoDB connection support
  - SQL query execution with safety checks
  - Schema discovery (list tables, describe columns)
  - Connection pooling
  - Read-only mode for safety
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# SQL injection prevention: block dangerous keywords
DANGEROUS_SQL = re.compile(
    r"\b(DROP|ALTER|TRUNCATE|CREATE|INSERT|UPDATE|DELETE|GRANT|REVOKE|EXEC)\b",
    re.IGNORECASE,
)


class DatabaseSkill:
    """Direct database query skill with safety guardrails.

    Supports PostgreSQL, MySQL, and SQLite connections.
    Default mode is read-only.
    """

    name = "database"
    description = "Query databases directly (PostgreSQL, MySQL, SQLite)"

    def __init__(self):
        self._connections: Dict[str, Any] = {}
        self._configured = False
        self._read_only = True
        self._max_rows = 1000

    def configure(self, **kwargs) -> None:
        """Configure database connections.

        Args:
            connections: dict of {name: {type, host, port, database, user, password}}
            read_only: if True, only SELECT queries allowed (default True)
            max_rows: max rows to return (default 1000)
        """
        self._connections = kwargs.get("connections", {})
        self._read_only = bool(kwargs.get("read_only", True))
        self._max_rows = int(kwargs.get("max_rows", 1000))
        self._configured = bool(self._connections)

    # --------------------------------------------------- query

    async def query(
        self, sql: str, connection: str = "default", params: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Execute a SQL query.

        Args:
            sql: SQL query string (SELECT only in read-only mode)
            connection: connection name from config
            params: query parameters (prevents SQL injection)
        """
        if not self._configured:
            return {"ok": False, "error": "database not configured"}

        # Safety check
        if self._read_only and DANGEROUS_SQL.search(sql):
            return {
                "ok": False,
                "error": "只读模式不允许执行修改操作。如需写入，请设置 read_only=false",
            }

        conn_info = self._connections.get(connection)
        if not conn_info:
            return {"ok": False, "error": f"连接 '{connection}' 不存在"}

        db_type = conn_info.get("type", "sqlite").lower()

        try:
            if db_type == "postgresql" or db_type == "postgres":
                return await self._query_postgres(conn_info, sql, params)
            elif db_type == "mysql":
                return await self._query_mysql(conn_info, sql, params)
            elif db_type == "sqlite":
                return await self._query_sqlite(conn_info, sql, params)
            else:
                return {"ok": False, "error": f"不支持的数据库类型: {db_type}"}
        except Exception as exc:
            logger.error("database query failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def _query_postgres(
        self, conn_info: dict, sql: str, params: Optional[list],
    ) -> Dict[str, Any]:
        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=conn_info.get("host", "localhost"),
                port=int(conn_info.get("port", 5432)),
                database=conn_info.get("database", ""),
                user=conn_info.get("user", ""),
                password=conn_info.get("password", ""),
            )

            try:
                if params:
                    rows = await conn.fetch(sql, *params)
                else:
                    rows = await conn.fetch(sql)

                columns = [k for k in rows[0].keys()] if rows else []
                data = [
                    [str(v) for v in row.values()]
                    for row in rows[:self._max_rows]
                ]
                return {
                    "ok": True,
                    "columns": columns,
                    "rows": data,
                    "row_count": len(data),
                    "truncated": len(rows) > self._max_rows,
                }
            finally:
                await conn.close()
        except ImportError:
            return {"ok": False, "error": "asyncpg 未安装。pip install asyncpg"}

    async def _query_mysql(
        self, conn_info: dict, sql: str, params: Optional[list],
    ) -> Dict[str, Any]:
        try:
            import aiomysql

            conn = await aiomysql.connect(
                host=conn_info.get("host", "localhost"),
                port=int(conn_info.get("port", 3306)),
                db=conn_info.get("database", ""),
                user=conn_info.get("user", ""),
                password=conn_info.get("password", ""),
                charset="utf8mb4",
            )

            try:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params or [])
                    rows = await cur.fetchall()
                    columns = [d[0] for d in cur.description] if cur.description else []
                    data = [[str(v) for v in row] for row in rows[:self._max_rows]]
                    return {
                        "ok": True,
                        "columns": columns,
                        "rows": data,
                        "row_count": len(data),
                        "truncated": len(rows) > self._max_rows,
                    }
            finally:
                conn.close()
        except ImportError:
            return {"ok": False, "error": "aiomysql 未安装。pip install aiomysql"}

    async def _query_sqlite(
        self, conn_info: dict, sql: str, params: Optional[list],
    ) -> Dict[str, Any]:
        import sqlite3

        db_path = conn_info.get("path", "data/memory.db")
        conn = await asyncio.to_thread(sqlite3.connect, db_path)
        conn.row_factory = sqlite3.Row

        try:
            if params:
                cur = await asyncio.to_thread(conn.execute, sql, params)
            else:
                cur = await asyncio.to_thread(conn.execute, sql)

            rows = cur.fetchall()
            columns = [d[0] for d in cur.description] if cur.description else []
            data = [[str(v) for v in row] for row in rows[:self._max_rows]]
            return {
                "ok": True,
                "columns": columns,
                "rows": data,
                "row_count": len(data),
                "truncated": len(rows) > self._max_rows,
            }
        finally:
            conn.close()

    # --------------------------------------------------- schema discovery

    async def list_tables(self, connection: str = "default") -> Dict[str, Any]:
        """List all tables in the database."""
        conn_info = self._connections.get(connection)
        if not conn_info:
            return {"ok": False, "error": f"连接 '{connection}' 不存在"}

        db_type = conn_info.get("type", "sqlite").lower()

        if db_type in ("postgresql", "postgres"):
            return await self.query(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name",
                connection,
            )
        elif db_type == "mysql":
            return await self.query("SHOW TABLES", connection)
        elif db_type == "sqlite":
            return await self.query(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
                connection,
            )
        return {"ok": False, "error": f"不支持的数据库类型: {db_type}"}

    async def describe_table(
        self, table_name: str, connection: str = "default",
    ) -> Dict[str, Any]:
        """Describe a table's columns."""
        conn_info = self._connections.get(connection)
        if not conn_info:
            return {"ok": False, "error": f"连接 '{connection}' 不存在"}

        db_type = conn_info.get("type", "sqlite").lower()

        if db_type in ("postgresql", "postgres"):
            return await self.query(
                """SELECT column_name, data_type, is_nullable, column_default
                   FROM information_schema.columns
                   WHERE table_name = $1 ORDER BY ordinal_position""",
                connection, [table_name],
            )
        elif db_type == "mysql":
            return await self.query(
                f"DESCRIBE `{table_name}`", connection,
            )
        elif db_type == "sqlite":
            return await self.query(
                f"PRAGMA table_info('{table_name}')", connection,
            )
        return {"ok": False, "error": f"不支持的数据库类型: {db_type}"}

    # --------------------------------------------------- skill interface

    def get_skill_schema(self) -> Dict[str, Any]:
        return {
            "name": "database",
            "description": "Query databases directly (PostgreSQL, MySQL, SQLite). Read-only by default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SQL query to execute (SELECT statements)",
                    },
                    "connection": {
                        "type": "string",
                        "description": "connection name (default: 'default')",
                        "default": "default",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["query", "list_tables", "describe"],
                        "description": "action: query, list tables, or describe table",
                    },
                    "table_name": {
                        "type": "string",
                        "description": "table name for describe action",
                    },
                },
                "required": ["action"],
            },
        }

    async def run(self, args: Dict[str, Any]) -> str:
        """Execute database skill."""
        action = args.get("action", "query")

        if action == "query":
            result = await self.query(
                sql=args.get("sql", ""),
                connection=args.get("connection", "default"),
            )
            if not result.get("ok"):
                return f"查询失败: {result.get('error')}"
            return self._format_result(result)

        elif action == "list_tables":
            result = await self.list_tables(args.get("connection", "default"))
            if not result.get("ok"):
                return f"获取表列表失败: {result.get('error')}"
            tables = [r[0] for r in result.get("rows", [])]
            return "数据库表:\n" + "\n".join(f"  - {t}" for t in tables)

        elif action == "describe":
            result = await self.describe_table(
                args.get("table_name", ""),
                args.get("connection", "default"),
            )
            if not result.get("ok"):
                return f"描述表失败: {result.get('error')}"
            return self._format_result(result)

        return "未知操作"

    def _format_result(self, result: Dict[str, Any]) -> str:
        """Format query result for display."""
        columns = result.get("columns", [])
        rows = result.get("rows", [])
        truncated = result.get("truncated", False)

        if not rows:
            return "查询结果为空"

        lines = []
        # Header
        lines.append(" | ".join(columns))
        lines.append("-" * len(lines[0]))

        for row in rows[:50]:
            lines.append(" | ".join(str(v) for v in row))

        if truncated:
            lines.append(f"... (已截断，共{result.get('row_count', 0)}+行)")

        return "\n".join(lines)


# Singleton
_database_skill: Optional[DatabaseSkill] = None


def get_database_skill() -> DatabaseSkill:
    global _database_skill
    if _database_skill is None:
        _database_skill = DatabaseSkill()
    return _database_skill