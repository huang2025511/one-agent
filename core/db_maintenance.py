"""统一数据库维护 — 完整性检查 / VACUUM / 统计 / 自动备份轮换 / 可选加密。

v2.2.0 新增。背景：v2.1.0 把所有系统状态收拢到单一 ``one_agent.db``
后，"一个文件承载一切"意味着单点损坏的代价被放大，必须补齐配套的
维护闭环：

  - integrity_check  PRAGMA quick_check（doctor 与 /api/db/stats 复用）
  - vacuum           WAL checkpoint + VACUUM 回收空间（CLI 维护命令）
  - db_stats         表行数 / 库大小 / schema 版本 / 待迁移旧库 / 备份概要
  - run_auto_backup  每日定时备份到 {data_dir}/backups/，轮换保留 N 份；
                     设置 ONE_AGENT_DB_KEY 后备份内的数据库自动加密
  - DBMaintenancePlugin  订阅 scheduler 的 db_backup cron 事件执行上述备份

可选加密说明：
  - 备份加密：设置 ONE_AGENT_DB_KEY（任意口令）后，自动备份 zip 内的
    ``one_agent.db`` 以 Fernet(PBKDF2 派生密钥) 加密为
    ``one_agent.db.enc``；导入端检测到 .enc 且能取到同一口令时自动解密
    还原（core.backup_export.DataImporter）。口令丢失 = 备份不可恢复，
    属预期安全语义。
  - 运行库加密（SQLCipher）：``core.db.create_sqlite_connection`` 支持
    显式设置 ONE_AGENT_DB_CIPHER=1 且安装 pysqlcipher3 时对运行库启用
    透明加密。默认关闭，避免既有明文库 + 密钥组合导致启动失败。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db import create_sqlite_connection
from core.hub import _LEGACY_DBS, database_path, get_hub, resolve_data_dir

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = "backups"
DB_BACKUP_PREFIX = "one_agent_backup_"
DB_BACKUP_SUFFIX = ".zip"
DEFAULT_KEEP_BACKUPS = 7
DEFAULT_BACKUP_CRON = "30 4 * * *"  # 每天 04:30


# ============================================================
# 加密助手（Fernet + PBKDF2）
# ============================================================

def get_db_passphrase() -> Optional[str]:
    """读取备份加密口令（ONE_AGENT_DB_KEY）。未设置返回 None。"""
    return os.environ.get("ONE_AGENT_DB_KEY") or None


def derive_fernet_key(passphrase: str) -> bytes:
    """口令 → Fernet 密钥（PBKDF2-HMAC-SHA256，固定盐）。

    固定盐是有意取舍：同一口令在任何环境派生出同一密钥，备份才能跨
    环境恢复；对口令强度而非盐随机性的依赖与 SQLCipher PRAGMA key
    的原生派生行为一致。
    """
    import base64
    import hashlib

    raw = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"),
        b"one-agent-db-backup-v1", 200_000,
    )
    return base64.urlsafe_b64encode(raw)


def _cryptography_available() -> bool:
    try:
        import cryptography  # noqa: F401

        return True
    except ImportError:
        return False


def encrypt_blob(data: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet

    return Fernet(derive_fernet_key(passphrase)).encrypt(data)


def decrypt_blob(token: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken

    try:
        return Fernet(derive_fernet_key(passphrase)).decrypt(token)
    except InvalidToken as exc:
        raise ValueError(
            "备份解密失败：ONE_AGENT_DB_KEY 与备份口令不一致，或备份已损坏"
        ) from exc


# ============================================================
# 完整性 / VACUUM / 统计
# ============================================================

def integrity_check(data_dir: Optional[str] = None) -> Dict[str, Any]:
    """PRAGMA quick_check（快速、非阻塞级联）+ WAL 残留体积。"""
    path = Path(database_path(data_dir or resolve_data_dir()))
    if not path.exists():
        return {"ok": False, "exists": False, "error": "database not found"}

    conn = None
    try:
        conn = create_sqlite_connection(str(path))
        rows = conn.execute("PRAGMA quick_check").fetchall()
        problems = [r[0] for r in rows if r[0] != "ok"]
        wal = Path(str(path) + "-wal")
        return {
            "ok": not problems,
            "exists": True,
            "problems": problems,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        }
    except sqlite3.Error as exc:
        return {"ok": False, "exists": True, "error": str(exc)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def vacuum(data_dir: Optional[str] = None) -> Dict[str, Any]:
    """checkpoint + VACUUM 回收空间。需要独占访问，建议停服或 CLI 执行。"""
    path = Path(database_path(data_dir or resolve_data_dir()))
    if not path.exists():
        return {"ok": False, "error": "database not found"}

    before = path.stat().st_size
    conn = None
    try:
        # 关闭进程内 Hub 单例连接，避免自身持有连接导致 VACUUM 锁死
        try:
            get_hub(str(path.parent)).close()
        except Exception:  # noqa: BLE001 — 单例可能不存在，忽略
            pass
        conn = create_sqlite_connection(str(path))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.isolation_level = None  # VACUUM 不能在显式事务内执行
        conn.execute("VACUUM")
        # WAL 模式下 VACUUM 的结果先写回 WAL，必须再 checkpoint 一次
        # 才会真正截断主文件（否则 page_count 缩了、文件大小不变）
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = path.stat().st_size
        return {
            "ok": True,
            "before_bytes": before,
            "after_bytes": after,
            "reclaimed_bytes": max(0, before - after),
        }
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc), "before_bytes": before}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def legacy_pending(data_dir: Optional[str] = None) -> List[str]:
    """仍待迁移的旧版分散库（含 .migrated 兜底文件）相对路径列表。"""
    dd = Path(data_dir or resolve_data_dir())
    out: List[str] = []
    for rel, _store in _LEGACY_DBS:
        if (dd / rel).exists():
            out.append(rel)
    return out


def db_stats(data_dir: Optional[str] = None) -> Dict[str, Any]:
    """统一库运行概要：大小 / 表行数 / schema 版本 / 待迁移 / 备份概要。"""
    dd = Path(data_dir or resolve_data_dir())
    path = Path(database_path(str(dd)))

    stats: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "data_dir": str(dd),
    }
    if not path.exists():
        return stats

    conn = None
    try:
        conn = create_sqlite_connection(str(path))
        tables: Dict[str, int] = {}
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            try:
                tables[name] = conn.execute(
                    f'SELECT count(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:
                tables[name] = -1  # FTS 影子表等极端情况下无法计数
        schema_versions = {
            r["store"]: r["version"] for r in conn.execute(
                "SELECT store, version FROM schema_versions")}
        wal = Path(str(path) + "-wal")
        shm = Path(str(path) + "-shm")
        stats.update({
            "size_bytes": path.stat().st_size,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "shm_bytes": shm.stat().st_size if shm.exists() else 0,
            "table_count": len(tables),
            "tables": tables,
            "schema_versions": schema_versions,
            "mode": oct(path.stat().st_mode & 0o777),
        })
    except sqlite3.Error as exc:
        stats["error"] = str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    stats["legacy_pending"] = legacy_pending(str(dd))
    backups = list_backups(str(dd))
    stats["backups"] = {
        "count": len(backups),
        "latest": backups[0] if backups else None,
    }
    stats["encrypted"] = bool(get_db_passphrase())
    return stats


# ============================================================
# 自动备份 + 轮换
# ============================================================

def _backup_dir(data_dir: str) -> Path:
    return Path(data_dir) / BACKUP_DIRNAME


def list_backups(data_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出现有自动备份（新→旧）。"""
    bdir = _backup_dir(data_dir or resolve_data_dir())
    if not bdir.is_dir():
        return []
    out = []
    for p in sorted(bdir.glob(DB_BACKUP_PREFIX + "*" + DB_BACKUP_SUFFIX),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append({
            "file": p.name,
            "path": str(p),
            "size_bytes": st.st_size,
            "created_at": st.st_mtime,
        })
    return out


def _encrypt_db_entry(zip_path: Path, passphrase: str) -> bool:
    """把备份 zip 内明文 one_agent.db 替换为 Fernet 加密的 .enc 条目。"""
    import zipfile

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if "one_agent.db" not in names:
            return False
        entries = {n: zf.read(n) for n in names}
    entries["one_agent.db.enc"] = encrypt_blob(entries.pop("one_agent.db"), passphrase)
    # manifest 标记加密，导入端据此选择解密路径
    import json

    if "manifest.json" in entries:
        try:
            manifest = json.loads(entries["manifest.json"])
        except ValueError:
            manifest = {}
        manifest["encrypted"] = True
        entries["manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")
    tmp = zip_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in entries.items():
            zf.writestr(name, blob)
    tmp.replace(zip_path)
    return True


def run_auto_backup(
    data_dir: Optional[str] = None,
    keep: int = DEFAULT_KEEP_BACKUPS,
    encrypt: Optional[bool] = None,
) -> Dict[str, Any]:
    """执行一次自动备份（checkpoint → zip → 可选加密 → 轮换）。

    Args:
        data_dir: 数据目录（默认统一解析）。
        keep: 轮换保留份数，<=0 表示不清理。
        encrypt: 是否加密库条目；None = 有 ONE_AGENT_DB_KEY 就加密。
    """
    from core.backup_export import DataExporter

    dd = str(data_dir or resolve_data_dir())
    bdir = _backup_dir(dd)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = bdir / f"{DB_BACKUP_PREFIX}{stamp}{DB_BACKUP_SUFFIX}"
    # 同秒内重复触发（如手动 API 连点）不能静默覆盖已有备份
    seq = 1
    while target.exists():
        target = bdir / f"{DB_BACKUP_PREFIX}{stamp}_{seq}{DB_BACKUP_SUFFIX}"
        seq += 1

    result: Dict[str, Any] = {"ok": False, "path": str(target), "encrypted": False}
    try:
        export = DataExporter(data_dir=dd).export_all(str(target))
        if not export.success:
            result["error"] = export.error or "export failed"
            return result

        passphrase = get_db_passphrase()
        do_encrypt = (passphrase is not None) if encrypt is None else encrypt
        if do_encrypt:
            if not passphrase:
                result["error"] = "encrypt requested but ONE_AGENT_DB_KEY not set"
                return result
            if not _cryptography_available():
                if encrypt is True:
                    # 显式要求加密：不静默降级为明文（安全语义优先）
                    result["error"] = (
                        "encrypt requested but 'cryptography' not installed "
                        "(pip install cryptography)")
                    return result
                # 自动模式：备份是安全网，降级明文 + 显式告警
                logger.warning(
                    "ONE_AGENT_DB_KEY set but cryptography unavailable — "
                    "backup stored UNENCRYPTED")
                result["warning"] = "cryptography unavailable, backup unencrypted"
            else:
                result["encrypted"] = _encrypt_db_entry(target, passphrase)

        result.update({
            "ok": True,
            "size_bytes": Path(target).stat().st_size,
            "items": export.items_exported,
            "duration_seconds": export.duration_seconds,
        })
        if keep and keep > 0:
            result["rotated"] = _rotate_backups(bdir, keep)
        logger.info("auto backup ok: %s (%d bytes, encrypted=%s)",
                    target, result["size_bytes"], result.get("encrypted", False))
    except Exception as exc:  # noqa: BLE001 — 备份失败必须兜底为结构化结果
        logger.error("auto backup failed: %s", exc, exc_info=True)
        result["error"] = str(exc)
    return result


def _rotate_backups(bdir: Path, keep: int) -> int:
    """删除超出保留份数的最旧备份，返回删除数。"""
    backups = sorted(bdir.glob(DB_BACKUP_PREFIX + "*" + DB_BACKUP_SUFFIX),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in backups[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("backup rotation unlink failed: %s", exc)
    return removed


# ============================================================
# Plugin — 订阅 scheduler 的 db_backup cron 事件
# ============================================================

class DBMaintenancePlugin:
    """每日自动备份插件（挂接 EventBus 的 cron 事件）。

    通过 scheduler.builtin_jobs 中的 ``db_backup`` 任务触发（默认
    ``30 4 * * *``），配置见 default_config.yaml::

        scheduler:
          db_maintenance:
            enabled: true
            keep_backups: 7
            encrypt_backups: true   # ONE_AGENT_DB_KEY 存在时生效

    刻意实现鸭子类型而非继承 core.plugin.Plugin：PluginManager 的
    register() 只依赖 name/setup/start/stop 协议，且 stop() 基类行为
    （自动退订）对单一 cron 订阅没有额外价值，直接手写更透明。
    """

    name = "db_maintenance"
    depends_on = ["scheduler"]
    load_priority = -10  # 晚于 scheduler 注册，确保事件订阅在启动期生效

    def __init__(self) -> None:
        self.ctx: Any = None
        self.bus: Any = None
        self._cfg: Dict[str, Any] = {}
        self._subscribed: List[tuple] = []
        self._running = False  # 防重入：备份进行中忽略重复触发

    async def setup(self, ctx) -> None:  # noqa: ANN001 — 与 Plugin 协议一致
        self.ctx = ctx
        self.bus = ctx.bus
        sched_cfg = (ctx.config.get("scheduler") or {}) if isinstance(
            ctx.config, dict) else {}
        self._cfg = sched_cfg.get("db_maintenance") or {}
        if self.bus is not None:
            self.bus.subscribe("cron", self._on_cron)
            self._subscribed.append(("cron", self._on_cron))
        logger.info("%s setup (enabled=%s)", self.name, self._enabled())

    async def start(self) -> None:
        logger.info("%s started", self.name)

    async def stop(self) -> None:
        if self.bus is not None:
            for event_type, handler in self._subscribed:
                try:
                    self.bus.unsubscribe(event_type, handler)
                except Exception:  # noqa: BLE001
                    pass
        self._subscribed.clear()
        logger.info("%s stopped", self.name)

    def _enabled(self) -> bool:
        return bool(self._cfg.get("enabled", True))

    async def _on_cron(self, event: Any) -> None:
        name = event.get("name") if hasattr(event, "get") else ""
        if name != "db_backup" or not self._enabled() or self._running:
            return
        self._running = True
        try:
            cfg = self.ctx.config if isinstance(self.ctx.config, dict) else {}
            data_dir = resolve_data_dir(cfg)
            keep = int(self._cfg.get("keep_backups", DEFAULT_KEEP_BACKUPS))
            encrypt_cfg = self._cfg.get("encrypt_backups", True)
            encrypt = None if encrypt_cfg else False  # 默认跟随密钥存在性
            result = await asyncio.to_thread(
                run_auto_backup, data_dir, keep, encrypt)
            if not result.get("ok"):
                logger.warning("scheduled db backup failed: %s", result.get("error"))
        except Exception as exc:  # noqa: BLE001
            logger.error("scheduled db backup crashed: %s", exc, exc_info=True)
        finally:
            self._running = False
