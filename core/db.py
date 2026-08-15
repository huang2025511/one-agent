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

# 可选运行库加密（SQLCipher）：
#   显式设置 ONE_AGENT_DB_CIPHER=1 且安装 pysqlcipher3 时，所有经工厂
#   创建的连接使用 PRAGMA key 透明加密。默认关闭 —— 既有明文库在设置
#   密钥后会无法打开，必须由用户显式选择开启（先备份再加密迁移）。
_CIPHER_MODULE: Optional[str] = None
if os.environ.get("ONE_AGENT_DB_CIPHER", "").strip() in ("1", "true", "yes"):
    try:
        import pysqlcipher3 as _pysqlcipher  # type: ignore

        _CIPHER_MODULE = "pysqlcipher3"
        _sqlite_connect = _pysqlcipher.sqlite3.connect  # type: ignore[attr-defined]
    except ImportError:
        raise RuntimeError(
            "ONE_AGENT_DB_CIPHER=1 但未安装 pysqlcipher3。"
            "运行 pip install pysqlcipher3 或取消该环境变量。"
        ) from None
else:
    _sqlite_connect = sqlite3.connect


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

    conn = _sqlite_connect(
        db_path, check_same_thread=False, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    cipher_key = os.environ.get("ONE_AGENT_DB_KEY")
    if _CIPHER_MODULE and cipher_key:
        # SQLCipher 要求 key 是首个 PRAGMA；密钥错误在此即抛出
        conn.execute(f"PRAGMA key=\"{cipher_key}\"")
        conn.execute("PRAGMA cipher_page_size=4096")
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
