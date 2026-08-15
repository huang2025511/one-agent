"""Config backup → unified DB + leaf-store default path tests (v2.1.0 数据统一).

覆盖：
  - ConfigBackupManager 备份存统一库 config_backups 表（create/list/
    restore/delete/rotate）
  - 旧版 config/backups/*.yaml 一次性迁移 + 目录改名 backups.migrated
  - 叶子存储模块默认路径惰性解析到 database_path()（ONE_AGENT_DATA_DIR
    在模块导入后设置仍然生效）
  - monitor.health 探测统一库
"""

import sqlite3
import time
from pathlib import Path

import pytest

from core.hub import close_hub, database_path, get_hub


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """隔离的数据目录：ONE_AGENT_DATA_DIR 指向 tmp。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(d))
    yield d
    close_hub(str(d))


def _unified_conn(data_dir) -> sqlite3.Connection:
    return sqlite3.connect(database_path(str(data_dir)))


class TestConfigBackupUnifiedDb:
    """备份内容入统一库，保留策略与签名兼容。"""

    def _make_config(self, data_dir, name: str = "test", retries: int = 3) -> Path:
        cfg_dir = data_dir / "config"
        cfg_dir.mkdir(exist_ok=True)
        cfg = cfg_dir / "config.yaml"
        cfg.write_text(
            f"agent:\n  name: {name}\n  retries: {retries}\n", encoding="utf-8")
        return cfg

    def test_create_and_list(self, data_dir):
        from config_backup import ConfigBackupManager

        cfg = self._make_config(data_dir)
        mgr = ConfigBackupManager(str(cfg))

        name = mgr.create_backup(reason="manual")
        assert name is not None and name.startswith("config_") and name.endswith(".yaml")

        listed = mgr.list_backups()
        assert len(listed) == 1
        entry = listed[0]
        assert entry["filename"] == name
        assert entry["reason"] == "manual"
        assert entry["size_bytes"] == cfg.stat().st_size
        assert entry["timestamp"] > 0
        assert entry["datetime"]

        # 内容确实在统一库 config_backups 表中，且不再写备份目录
        with _unified_conn(data_dir) as conn:
            row = conn.execute(
                "SELECT content FROM config_backups WHERE name = ?", (name,)).fetchone()
        assert row is not None
        assert "name: test" in row[0]
        assert not (cfg.parent / "backups").exists()

    def test_get_content_and_restore(self, data_dir):
        from config_backup import ConfigBackupManager

        cfg = self._make_config(data_dir)
        mgr = ConfigBackupManager(str(cfg))
        name = mgr.create_backup(reason="pre-change")

        assert mgr.get_backup_content(name) == {
            "agent": {"name": "test", "retries": 3}}
        assert mgr.get_backup_content("missing.yaml") is None

        cfg.write_text("agent:\n  name: changed\n", encoding="utf-8")
        assert mgr.restore_backup(name) is True
        assert "name: test" in cfg.read_text(encoding="utf-8")
        # restore 前自动创建了 pre-restore 备份
        reasons = [b["reason"] for b in mgr.list_backups()]
        assert "pre-restore" in reasons

        assert mgr.restore_backup("missing.yaml") is False

    def test_delete_backup(self, data_dir):
        from config_backup import ConfigBackupManager

        cfg = self._make_config(data_dir)
        mgr = ConfigBackupManager(str(cfg))
        name = mgr.create_backup(reason="manual")

        assert mgr.delete_backup(name) is True
        assert mgr.delete_backup(name) is False
        assert mgr.list_backups() == []

    def test_rotation_keeps_last_10(self, data_dir):
        from config_backup import ConfigBackupManager

        cfg = self._make_config(data_dir)
        mgr = ConfigBackupManager(str(cfg))

        names = []
        for i in range(13):
            name = mgr.create_backup(reason=f"bulk{i}")
            assert name is not None
            names.append(name)
            # 同秒内同名覆盖（与旧文件实现一致），错开时间戳保证版本数
            time.sleep(0.012)

        listed = mgr.list_backups()
        assert len(listed) == mgr._max_backups
        filenames = {b["filename"] for b in listed}
        assert set(names[:13 - mgr._max_backups]).isdisjoint(filenames)  # 最旧被轮转
        assert names[-1] in filenames  # 最新保留

    def test_diff_with_current(self, data_dir):
        from config_backup import ConfigBackupManager

        cfg = self._make_config(data_dir)
        mgr = ConfigBackupManager(str(cfg))
        name = mgr.create_backup(reason="manual")

        cfg.write_text("agent:\n  name: test\n  retries: 9\nextra: 1\n", encoding="utf-8")
        diff = mgr.diff_with_current(name)
        assert diff is not None
        assert "modified" in diff and "added" in diff and "removed" in diff


class TestLegacyBackupMigration:
    """旧版 config/backups/*.yaml 一次性迁入统一库。"""

    def _setup_legacy(self, data_dir) -> Path:
        cfg_dir = data_dir / "config"
        cfg_dir.mkdir(exist_ok=True)
        cfg = cfg_dir / "config.yaml"
        cfg.write_text("agent:\n  name: now\n", encoding="utf-8")

        bak = cfg_dir / "backups"
        bak.mkdir()
        (bak / "config_20240101_000000_manual.yaml").write_text(
            "agent:\n  name: old1\n", encoding="utf-8")
        (bak / "config_20240102_000000_pre-change.yaml").write_text(
            "agent:\n  name: old2\n", encoding="utf-8")
        (bak / "backup_index.json").write_text(
            '[{"filename": "config_20240101_000000_manual.yaml", '
            '"reason": "manual", "timestamp": 1, "datetime": "x", "size_bytes": 1}]',
            encoding="utf-8")
        return cfg

    def test_migrate_legacy_dir(self, data_dir):
        from config_backup import ConfigBackupManager

        cfg = self._setup_legacy(data_dir)
        mgr = ConfigBackupManager(str(cfg))

        # 目录改名 backups.migrated 兜底
        assert not (cfg.parent / "backups").is_dir()
        assert (cfg.parent / "backups.migrated").is_dir()

        listed = mgr.list_backups()
        assert len(listed) == 2
        # 按文件名时间正序；reason 从 backup_index.json 恢复，无记录时从文件名解析
        assert listed[0]["filename"] == "config_20240101_000000_manual.yaml"
        assert listed[0]["reason"] == "manual"
        assert listed[1]["reason"] == "pre-change"
        assert mgr.get_backup_content(listed[0]["filename"]) == {"agent": {"name": "old1"}}

    def test_migration_is_idempotent(self, data_dir):
        from config_backup import ConfigBackupManager

        cfg = self._setup_legacy(data_dir)
        ConfigBackupManager(str(cfg))
        # 二次初始化：表中已有备份，backups.migrated 不再动，无重复导入
        mgr = ConfigBackupManager(str(cfg))
        assert len(mgr.list_backups()) == 2

    def test_no_migration_when_db_has_backups(self, data_dir):
        from config_backup import ConfigBackupManager

        cfg = self._setup_legacy(data_dir)
        # 先让库里已有备份（模拟其他实例已建），旧目录原样保留兜底
        get_hub(str(data_dir)).backup_put(
            "config_20250101_000000_manual.yaml", "agent: {}\n", reason="manual")
        ConfigBackupManager(str(cfg))
        assert (cfg.parent / "backups").is_dir()  # 未改名
        assert len(ConfigBackupManager(str(cfg)).list_backups()) == 1


class TestLazyDefaultPaths:
    """叶子存储默认路径在 __init__ 时惰性解析 → 统一库。"""

    def test_defaults_resolve_to_unified_db(self, data_dir):
        from core.audit_log import AuditLog
        from core.eval import EvalHarness
        from core.task_scheduler import AsyncTaskScheduler, TaskStore
        from memory.user_profile import UserProfileStore
        from models.cost_tracker import CostTracker
        from skills.document_search import DocumentStore

        expected = str(Path(database_path(str(data_dir))).resolve())

        a = AuditLog()
        got = a._conn.execute("PRAGMA database_list").fetchall()[0][2]
        assert str(Path(got).resolve()) == expected
        a.close()

        up = UserProfileStore()
        assert str(Path(up.db_path).resolve()) == expected
        up.close()

        for store in (TaskStore(), AsyncTaskScheduler()._store):
            got = store._conn.execute("PRAGMA database_list").fetchall()[0][2]
            assert str(Path(got).resolve()) == expected
            store.close()

        for inst in (EvalHarness(), CostTracker(), DocumentStore()):
            got = inst._conn.execute("PRAGMA database_list").fetchall()[0][2]
            assert str(Path(got).resolve()) == expected
            inst.close()

    def test_explicit_path_still_wins(self, data_dir):
        from core.audit_log import AuditLog

        explicit = data_dir / "standalone" / "audit.db"
        a = AuditLog(str(explicit))
        got = a._conn.execute("PRAGMA database_list").fetchall()[0][2]
        assert str(Path(got).resolve()) == str(explicit.resolve())
        a.close()

    def test_tables_share_unified_db(self, data_dir):
        from core.audit_log import AuditLog
        from core.eval import EvalHarness
        from core.task_scheduler import TaskStore
        from memory.user_profile import UserProfileStore
        from models.cost_tracker import CostTracker

        for inst in (AuditLog(), UserProfileStore(), TaskStore(),
                     EvalHarness(), CostTracker()):
            inst.close()

        with _unified_conn(data_dir) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"audit_log", "preferences", "tasks",
                "eval_results", "cost_log"} <= tables


class TestHealthChecksUnifiedDb:
    """monitor.health 内置检查探测统一库，返回结构不变。"""

    def test_database_check_healthy(self, data_dir):
        from monitor.health import HealthStatus, _check_database

        # 统一库已由前面的 fixture/create 建立；无库时 UNHEALTHY
        get_hub(str(data_dir)).kv_set("ping", 1)
        check = _check_database()
        assert check.status == HealthStatus.HEALTHY
        assert "sessions" in check.message
        assert "session_count" in check.details

    def test_database_check_missing_db(self, tmp_path, monkeypatch):
        from monitor.health import HealthStatus, _check_database

        empty = tmp_path / "nope"
        empty.mkdir()
        monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(empty))
        try:
            check = _check_database()
            assert check.status == HealthStatus.UNHEALTHY
            assert "not found" in check.message
        finally:
            close_hub(str(empty))

    def test_memory_check_counts_unified_tables(self, data_dir):
        from monitor.health import HealthStatus, _check_memory

        hub = get_hub(str(data_dir))
        with hub._conn as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS entities (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO entities(name) VALUES ('e1')")
            conn.commit()

        check = _check_memory()
        assert check.status == HealthStatus.HEALTHY
        assert check.details["entities"] == 1
        assert check.details["embeddings"] == 0  # 表不存在 → 0，而非报错
        assert "Entities" in check.message and "Embeddings" in check.message
