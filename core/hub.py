"""统一数据库中枢 — 所有系统状态的单一事实源。

架构（v2.1.0 数据统一）：
    {data_dir}/one_agent.db  ←  唯一需要迁移的文件

    收拢内容：
      - settings          配置（原 config.db，YAML 仅首启种子）
      - sessions/messages 聊天记录
      - memory/roles/kg/embeddings/user_profile 记忆体系
      - approvals/audit_log/tasks/eval_*/failures 流程数据
      - cost_log          成本追踪
      - kv                杂项键值（重启标记、市场注册表…）
      - stored_files      技能/市场包文件（启动时物化到磁盘）
      - config_backups    配置备份
      - schema_versions   按 store 名的 schema 版本（替代 user_version）

    刻意留在库外（可重建、非状态）：
      - logs/             运行日志
      - workspace/        执行器临时工作区
      - skills/builtin/   随代码分发的内置技能

设计要点：
  - 各模块仍持有独立连接指向同一文件（WAL 多读单写 + busy_timeout），
    表名互不冲突，模块间零耦合。
  - PRAGMA user_version 每库文件仅一个值，无法支撑多 store 共库，
    改用 schema_versions(store, version) 表；打开旧版独立库文件时
    自动继承其 user_version（向后兼容）。
  - 旧版分散库（config.db、memory/*.db）首次启动自动 ATTACH 迁移：
    克隆 DDL → 空表才复制行 → 记录 schema 版本 → 原文件改名
    *.migrated 保留兜底。迁移环境 = 复制 one_agent.db。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from core.db import create_sqlite_connection

logger = logging.getLogger(__name__)

_DB_FILENAME = "one_agent.db"

# 旧版分散库 → (相对路径, BaseSQLiteStore 子类的 store key)。
# store key 为 None 表示该模块无 schema 版本管理（仅建表）。
_LEGACY_DBS: List[tuple] = [
    ("config.db", None),                    # settings（ConfigStore）
    ("memory/sessions.db", "SessionStore"),
    ("memory/longterm.sqlite", "LongTermMemory"),
    ("memory/kg.db", "KnowledgeGraph"),
    ("memory/embeddings.db", "EmbeddingStore"),
    ("memory/roles.db", "RoleStore"),
    ("memory/user_profile.db", None),
    ("memory/costs.db", None),
    ("memory/approvals.db", None),
    ("memory/audit.db", None),
    ("memory/improvements.db", None),
    ("memory/tasks.db", None),
    ("memory/eval_results.db", None),
    ("memory/docs.db", None),
]

_CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions (
    store   TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stored_files (
    package    TEXT NOT NULL,
    path       TEXT NOT NULL,
    content    BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (package, path)
);
CREATE TABLE IF NOT EXISTS config_backups (
    name       TEXT PRIMARY KEY,
    reason     TEXT,
    content    TEXT NOT NULL,
    size_bytes INTEGER,
    created_at REAL NOT NULL
)
"""


# ============================================================
# 路径解析
# ============================================================

def resolve_data_dir(cfg: Optional[Dict[str, Any]] = None) -> str:
    """解析数据目录：ONE_AGENT_DATA_DIR 环境变量 > agent.data_dir > ./data。"""
    env = os.environ.get("ONE_AGENT_DATA_DIR")
    if env:
        return env
    if cfg:
        agent = cfg.get("agent") or {}
        if isinstance(agent, dict) and agent.get("data_dir"):
            return str(agent["data_dir"])
    return "./data"


def database_path(data_dir: Optional[str] = None) -> str:
    """统一库路径：{data_dir}/one_agent.db。"""
    return str(Path(data_dir or resolve_data_dir()) / _DB_FILENAME)


# ============================================================
# schema 版本（供 BaseSQLiteStore 等共库 store 使用）
# ============================================================

def ensure_core_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_CORE_SCHEMA)
    conn.commit()


def get_store_schema_version(conn: sqlite3.Connection, store: str) -> int:
    """读取 store 的 schema 版本。

    兼容：schema_versions 无记录且文件 user_version > 0 时，视为旧版
    独立库文件，继承 user_version 并落表（此后 user_version 不再使用）。
    """
    ensure_core_tables(conn)
    row = conn.execute(
        "SELECT version FROM schema_versions WHERE store = ?", (store,)).fetchone()
    if row is not None:
        return int(row[0])
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    if uv:
        conn.execute(
            "INSERT OR REPLACE INTO schema_versions(store, version) VALUES (?, ?)",
            (store, int(uv)))
        conn.commit()
        return int(uv)
    return 0


def set_store_schema_version(conn: sqlite3.Connection, store: str, version: int) -> None:
    ensure_core_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_versions(store, version) VALUES (?, ?)",
        (store, int(version)))
    conn.commit()


# ============================================================
# 旧库迁移
# ============================================================

def _table_names(conn: sqlite3.Connection, schema: str) -> List[Dict[str, Any]]:
    return list(conn.execute(
        f"SELECT type, name, sql FROM {schema}.sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"))


def _migrate_one(conn: sqlite3.Connection, legacy_path: Path, store: Optional[str]) -> int:
    """单个旧库迁入统一库（已 ATTACH 为 legacy）。返回复制的表数。"""
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy_path),))
        attached = True
        objs = _table_names(conn, "legacy")
        # FTS5 虚拟表的影子表不迁移，虚拟表重建时自动生成。
        # 影子表名固定为 _data/_idx/_content/_docsize/_config 五种，
        # 不能按 "表名前缀+" 泛匹配 —— 那会把 memory_weights 这类
        # 真实业务表误判成影子表。
        shadow = {f"{v}_{s}" for v in
                  {r["name"] for r in objs
                   if r["type"] == "table" and "CREATE VIRTUAL" in r["sql"].upper()}
                  for s in ("data", "idx", "content", "docsize", "config")}
        vtabs = {r["name"] for r in objs
                 if r["type"] == "table" and "CREATE VIRTUAL" in r["sql"].upper()}
        copied = 0
        for r in objs:
            if r["type"] != "table":
                continue
            name = r["name"]
            if name in shadow:
                continue
            ddl = r["sql"].replace(
                "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1).replace(
                "CREATE VIRTUAL TABLE ", "CREATE VIRTUAL TABLE IF NOT EXISTS ", 1)
            conn.execute(ddl)
            # 幂等：目标表已有数据（前次迁移/正常运行写入）则跳过复制
            cnt = conn.execute(
                f'SELECT count(*) FROM main."{name}"').fetchone()[0]
            if cnt:
                continue
            info = list(conn.execute(f'PRAGMA main.table_info("{name}")'))
            cols = ", ".join(f'"{c["name"]}"' for c in info)
            has_pk = any(c["pk"] for c in info)
            try:
                if has_pk:
                    conn.execute(
                        f'INSERT OR IGNORE INTO main."{name}" ({cols}) '
                        f'SELECT {cols} FROM legacy."{name}"')
                else:
                    # 无 INTEGER PRIMARY KEY 别名的表（FTS5/普通 rowid 表）
                    # 显式带 rowid 复制，保持 memory ↔ memory_weights 的
                    # rowid 关联不断裂
                    conn.execute(
                        f'INSERT OR IGNORE INTO main."{name}" (rowid, {cols}) '
                        f'SELECT rowid, {cols} FROM legacy."{name}"')
                copied += 1
            except sqlite3.Error as exc:
                logger.warning("migrate table %s failed (skipped): %s", name, exc)
        if store:
            uv = conn.execute("PRAGMA legacy.user_version").fetchone()[0]
            if uv:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_versions(store, version) "
                    "VALUES (?, ?)", (store, int(uv)))
        conn.commit()
        return copied
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE legacy")
            except sqlite3.Error:
                pass


def _retire_legacy(path: Path) -> None:
    """迁移完成后退役旧文件：改名 .migrated，清理 wal/shm。"""
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        path.rename(path.with_name(path.name + ".migrated"))
    except OSError as exc:
        logger.warning("legacy file rename failed: %s", exc)


def migrate_legacy(data_dir: Optional[str] = None,
                   conn: Optional[sqlite3.Connection] = None) -> List[str]:
    """把旧版分散库全部迁入统一库。幂等，返回迁移报告（文件列表）。"""
    dd = Path(data_dir or resolve_data_dir())
    target = database_path(str(dd))
    own = conn is None
    if own:
        conn = create_sqlite_connection(target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    report: List[str] = []
    try:
        ensure_core_tables(conn)
        for rel, store in _LEGACY_DBS:
            legacy = dd / rel
            if not legacy.exists():
                continue
            try:
                copied = _migrate_one(conn, legacy, store)
                _retire_legacy(legacy)
                report.append(f"{rel} ({copied} tables)")
                logger.info("migrated legacy db %s -> %s (%d tables)", rel, target, copied)
            except Exception as exc:
                logger.warning("legacy migration failed for %s (left in place): %s",
                               rel, exc)
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass
    return report


# ============================================================
# Hub — kv / 文件包 / 备份 / checkpoint
# ============================================================

class Hub:
    """统一库的进程级入口（kv、文件包存储、配置备份、WAL checkpoint）。

    各业务 store 不经过 Hub 直接开连接即可（同库文件）；Hub 提供的是
    跨模块共享的通用存储能力。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.path = database_path(db_path) if _is_data_dir(db_path) else str(db_path)
        self._lock = threading.RLock()
        self._conn = create_sqlite_connection(self.path)
        # 统一库含 API key/凭据/聊天记录，与 config.db 同等敏感级别
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        ensure_core_tables(self._conn)

    # -- kv --------------------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return row["value"]

    def kv_set(self, key: str, value: Any) -> None:
        encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv(key, value, updated_at) VALUES (?, ?, ?)",
                (key, encoded, time.time()))
            self._conn.commit()

    def kv_delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            self._conn.commit()

    def kv_keys(self, prefix: Optional[str] = None) -> List[str]:
        with self._lock:
            if prefix:
                rows = self._conn.execute(
                    "SELECT key FROM kv WHERE key LIKE ? ORDER BY key",
                    (prefix + "%",)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT key FROM kv ORDER BY key").fetchall()
        return [r["key"] for r in rows]

    # -- 文件包（技能/市场包 → DB blob，启动时物化到磁盘） ----------------

    def files_put(self, package: str, files: Dict[str, Union[str, bytes]]) -> int:
        """整包写入（先清空该包）。files: {相对路径: 内容}。"""
        now = time.time()
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM stored_files WHERE package = ?", (package,))
                for path, content in files.items():
                    blob = content if isinstance(content, bytes) else str(content).encode("utf-8")
                    self._conn.execute(
                        "INSERT INTO stored_files(package, path, content, updated_at) "
                        "VALUES (?, ?, ?, ?)", (package, path, blob, now))
        return len(files)

    def files_get(self, package: str) -> Dict[str, bytes]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, content FROM stored_files WHERE package = ? ORDER BY path",
                (package,)).fetchall()
        return {r["path"]: bytes(r["content"]) for r in rows}

    def files_delete(self, package: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM stored_files WHERE package = ?", (package,))
            self._conn.commit()

    def packages(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT package FROM stored_files ORDER BY package").fetchall()
        return [r["package"] for r in rows]

    def capture_dir(self, package: str, src_dir: Union[str, Path],
                    skip: Optional[List[str]] = None) -> int:
        """磁盘目录 → DB 包（磁盘为源，用于存量数据首次入库）。"""
        root = Path(src_dir)
        if not root.is_dir():
            return 0
        default_skip = {"__pycache__", ".git", ".DS_Store"}
        default_skip.update(skip or [])
        files: Dict[str, bytes] = {}
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if any(part in default_skip for part in p.parts) or rel.endswith(".pyc"):
                continue
            files[rel] = p.read_bytes()
        if not files:
            return 0
        return self.files_put(package, files)

    def materialize(self, package: str, dst_dir: Union[str, Path]) -> int:
        """DB 包 → 磁盘目录（DB 为源；内容一致的文件不重写）。"""
        files = self.files_get(package)
        if not files:
            return 0
        root = Path(dst_dir)
        written = 0
        for rel, blob in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if target.is_file() and target.read_bytes() == blob:
                    continue
            except OSError:
                pass
            target.write_bytes(blob)
            written += 1
        return written

    # -- 配置备份 ---------------------------------------------------------

    def backup_put(self, name: str, content: str, reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO config_backups"
                "(name, reason, content, size_bytes, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, reason, content, len(content.encode("utf-8")), time.time()))
            self._conn.commit()

    def backup_get(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM config_backups WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def backup_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, reason, size_bytes, created_at FROM config_backups "
                "ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def backup_delete(self, name: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM config_backups WHERE name = ?", (name,))
            self._conn.commit()

    # -- 生命周期 ---------------------------------------------------------

    def checkpoint(self) -> None:
        """WAL 落盘截断 — 停机/备份前调用，保证拷贝单个 db 文件即完整。"""
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            logger.debug("wal checkpoint failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass


def _is_data_dir(path: Optional[str]) -> bool:
    """构造参数兼容：传入目录 → 定位库文件；传入文件路径 → 直接使用。"""
    if not path:
        return True
    return str(path).endswith(_DB_FILENAME) is False and Path(path).suffix == ""


_instances: Dict[str, Hub] = {}
_instances_lock = threading.Lock()


def get_hub(db_path_or_dir: Optional[str] = None) -> Hub:
    """获取（或创建）进程级 Hub 单例。"""
    if not db_path_or_dir:
        key = database_path()
    elif _is_data_dir(db_path_or_dir):
        key = database_path(db_path_or_dir)
    else:
        key = str(db_path_or_dir)
    key = str(Path(key).absolute())
    with _instances_lock:
        hub = _instances.get(key)
        if hub is None:
            hub = Hub(key)
            _instances[key] = hub
        return hub


def close_hub(db_path_or_dir: Optional[str] = None) -> None:
    """关闭 Hub 单例（测试用）：无参关闭全部。"""
    with _instances_lock:
        targets = list(_instances.items()) if db_path_or_dir is None else [
            (k, v) for k, v in _instances.items()
            if k == str(Path(
                database_path(db_path_or_dir) if _is_data_dir(db_path_or_dir)
                else db_path_or_dir  # type: ignore[arg-type]
            ).absolute())
        ]
        for k, v in targets:
            try:
                v.close()
            except Exception:
                pass
            _instances.pop(k, None)
