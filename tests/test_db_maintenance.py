"""统一数据库维护（v2.2.0）测试。

覆盖 core/db_maintenance.py：
- db_stats / integrity_check / vacuum
- 自动备份 + 轮换（list_backups / run_auto_backup）
- 备份加密（ONE_AGENT_DB_KEY + Fernet）roundtrip 与失败路径
- DataImporter restore_db 整库还原（跨环境部署）
- hub.migrate_legacy(dry_run=True) 只读预览
- DBMaintenancePlugin 的 cron 事件触发
"""
import asyncio
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    dd = tmp_path / "data"
    dd.mkdir()
    monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(dd))
    monkeypatch.delenv("ONE_AGENT_DB_KEY", raising=False)
    yield dd
    from core.hub import close_hub
    close_hub()
    from core.config_store import close_config_store
    close_config_store()


def _seed_db(dd: Path) -> Path:
    """建统一库并写入若干 kv / sessions 数据。"""
    from core.hub import close_hub, get_hub
    hub = get_hub(str(dd))
    hub.kv_set("owner", "tester")
    close_hub()

    from core.hub import database_path
    from memory.session_store import SessionStore
    db = database_path(str(dd))
    store = SessionStore(db)
    store.create_session("s1", "hello")
    store.add_message("s1", "user", "你好")
    getattr(store, "close", lambda: None)()
    return Path(db)


class TestStatsAndIntegrity:
    def test_stats_reports_tables_and_backups(self, isolated_data_dir):
        _seed_db(isolated_data_dir)
        from core.db_maintenance import db_stats

        stats = db_stats(str(isolated_data_dir))
        assert stats["exists"] is True
        assert stats["size_bytes"] > 0
        assert "kv" in stats["tables"]
        assert "sessions" in stats["tables"]
        assert stats["tables"]["sessions"] == 1
        assert stats["legacy_pending"] == []
        assert stats["backups"]["count"] == 0
        assert stats["encrypted"] is False  # 未设置 ONE_AGENT_DB_KEY

    def test_stats_on_missing_db(self, isolated_data_dir):
        from core.db_maintenance import db_stats

        stats = db_stats(str(isolated_data_dir))
        assert stats["exists"] is False

    def test_integrity_check_ok(self, isolated_data_dir):
        _seed_db(isolated_data_dir)
        from core.db_maintenance import integrity_check

        result = integrity_check(str(isolated_data_dir))
        assert result["ok"] is True
        assert result["problems"] == []

    def test_integrity_check_missing_db(self, isolated_data_dir):
        from core.db_maintenance import integrity_check

        result = integrity_check(str(isolated_data_dir))
        assert result["ok"] is False
        assert result["exists"] is False


class TestVacuum:
    def test_vacuum_reclaims_space(self, isolated_data_dir):
        from core.db import create_sqlite_connection
        from core.hub import database_path

        db = database_path(str(isolated_data_dir))
        conn = create_sqlite_connection(db)
        conn.execute("CREATE TABLE junk (x TEXT)")
        conn.executemany("INSERT INTO junk VALUES (?)",
                         [("x" * 200,) for _ in range(3000)])
        conn.commit()
        conn.execute("DELETE FROM junk")
        conn.commit()
        conn.close()

        from core.db_maintenance import vacuum
        result = vacuum(str(isolated_data_dir))
        assert result["ok"] is True
        # WAL 模式下 VACUUM 后必须再 checkpoint 才真正截断
        assert result["after_bytes"] < result["before_bytes"]

    def test_vacuum_missing_db(self, isolated_data_dir):
        from core.db_maintenance import vacuum

        result = vacuum(str(isolated_data_dir))
        assert result["ok"] is False


class TestAutoBackup:
    def test_backup_creates_zip_with_db(self, isolated_data_dir):
        _seed_db(isolated_data_dir)
        from core.db_maintenance import list_backups, run_auto_backup

        result = run_auto_backup(str(isolated_data_dir), keep=5)
        assert result["ok"] is True
        assert result["encrypted"] is False

        backups = list_backups(str(isolated_data_dir))
        assert len(backups) == 1
        with zipfile.ZipFile(result["path"]) as zf:
            assert "one_agent.db" in zf.namelist()
            assert "one_agent.db.enc" not in zf.namelist()

    def test_rotation_keeps_n(self, isolated_data_dir):
        from core.db_maintenance import list_backups, run_auto_backup

        for _ in range(4):
            r = run_auto_backup(str(isolated_data_dir), keep=2)
            assert r["ok"] is True
            assert r["rotated"] is not None

        # 同秒内多次备份文件名相同会互相覆盖 — 用列表长度断言轮换上界
        assert len(list_backups(str(isolated_data_dir))) <= 2

    def test_backup_rotation_removes_oldest(self, isolated_data_dir):
        import time as _t

        from core.db_maintenance import list_backups, run_auto_backup

        keep = 2
        for i in range(3):
            # 手工改 mtime 拉开时间差，确保文件名/顺序可区分
            r = run_auto_backup(str(isolated_data_dir), keep=keep)
            assert r["ok"]
            p = Path(r["path"])
            old = _t.time() - (100 - i * 10)
            os.utime(p, (old, old))

        backups = list_backups(str(isolated_data_dir))
        assert len(backups) == keep


class TestEncryptedBackup:
    def _has_crypto(self):
        try:
            import cryptography  # noqa: F401
            return True
        except ImportError:
            return False

    def test_encrypted_roundtrip_across_envs(self, isolated_data_dir, tmp_path, monkeypatch):
        if not self._has_crypto():
            pytest.skip("cryptography 未安装")
        _seed_db(isolated_data_dir)
        monkeypatch.setenv("ONE_AGENT_DB_KEY", "roundtrip-pass")

        from core.db_maintenance import run_auto_backup
        result = run_auto_backup(str(isolated_data_dir), keep=3)
        assert result["ok"] is True
        assert result["encrypted"] is True

        # 明文库不在 zip 里
        with zipfile.ZipFile(result["path"]) as zf:
            names = zf.namelist()
            assert "one_agent.db.enc" in names
            assert "one_agent.db" not in names
            assert json.loads(zf.read("manifest.json")).get("encrypted") is True

        # 新环境（不同 data_dir）用同一口令整库还原
        target = tmp_path / "new_env"
        target.mkdir()
        from core.backup_export import DataImporter
        res = DataImporter(data_dir=str(target)).import_from_file(
            result["path"], restore_db=True)
        assert res.success, res.error
        assert res.items_imported.get("database_restored") == 1

        conn = sqlite3.connect(str(target / "one_agent.db"))
        assert conn.execute(
            "SELECT value FROM kv WHERE key='owner'").fetchone()[0] == "tester"
        conn.close()

    def test_wrong_passphrase_rejected(self, isolated_data_dir, tmp_path, monkeypatch):
        if not self._has_crypto():
            pytest.skip("cryptography 未安装")
        _seed_db(isolated_data_dir)
        monkeypatch.setenv("ONE_AGENT_DB_KEY", "right-pass")
        from core.db_maintenance import run_auto_backup
        result = run_auto_backup(str(isolated_data_dir), keep=3)
        assert result["encrypted"] is True

        monkeypatch.setenv("ONE_AGENT_DB_KEY", "wrong-pass")
        from core.backup_export import DataImporter
        target = tmp_path / "env2"
        target.mkdir()
        res = DataImporter(data_dir=str(target)).import_from_file(
            result["path"], restore_db=True)
        assert not res.success
        assert "ONE_AGENT_DB_KEY" in res.error

    def test_encrypted_without_key_rejected(self, isolated_data_dir, tmp_path, monkeypatch):
        if not self._has_crypto():
            pytest.skip("cryptography 未安装")
        _seed_db(isolated_data_dir)
        monkeypatch.setenv("ONE_AGENT_DB_KEY", "some-pass")
        from core.db_maintenance import run_auto_backup
        result = run_auto_backup(str(isolated_data_dir), keep=3)

        monkeypatch.delenv("ONE_AGENT_DB_KEY")
        from core.backup_export import DataImporter
        target = tmp_path / "env3"
        target.mkdir()
        res = DataImporter(data_dir=str(target)).import_from_file(
            result["path"], restore_db=True)
        assert not res.success


class TestMigrateDryRun:
    def test_dry_run_reports_without_mutation(self, isolated_data_dir):
        # 造一个旧版 sessions.db
        legacy = isolated_data_dir / "memory" / "sessions.db"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(legacy)
        conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT);
        INSERT INTO sessions VALUES ('s1', 'old');
        """)
        conn.commit()
        conn.close()

        from core.hub import migrate_legacy
        report = migrate_legacy(str(isolated_data_dir), dry_run=True)
        assert any("memory/sessions.db" in line for line in report)
        assert "1 rows" in report[0]

        # dry-run 不改不改名
        assert legacy.exists()
        assert not Path(str(legacy) + ".migrated").exists()
        from core.hub import database_path
        assert not Path(database_path(str(isolated_data_dir))).exists()


class TestDBMaintenancePlugin:
    def test_cron_db_backup_fires_backup(self, isolated_data_dir):
        _seed_db(isolated_data_dir)
        from core.db_maintenance import DBMaintenancePlugin, list_backups

        plugin = DBMaintenancePlugin()

        class _Bus:
            def __init__(self):
                self.subscriptions = []

            def subscribe(self, etype, handler):
                self.subscriptions.append((etype, handler))

            def unsubscribe(self, etype, handler):
                self.subscriptions.remove((etype, handler))

        class _Ctx:
            config = {"agent": {}, "scheduler": {"db_maintenance": {}}}

            def __init__(self, bus):
                self.bus = bus

        bus = _Bus()
        ctx = _Ctx(bus)
        asyncio.run(plugin.setup(ctx))
        assert ("cron", plugin._on_cron) in bus.subscriptions

        class _Event:
            def __init__(self, payload):
                self.payload = payload

            def get(self, key, default=None):
                return self.payload.get(key, default)

        asyncio.run(plugin._on_cron(_Event({"name": "db_backup"})))
        # 防重入标志已复位
        assert plugin._running is False
        backups = list_backups(str(isolated_data_dir))
        assert len(backups) == 1

        # 非 db_backup 的事件不触发
        asyncio.run(plugin._on_cron(_Event({"name": "other"})))
        assert len(list_backups(str(isolated_data_dir))) == 1

        asyncio.run(plugin.stop())
        assert bus.subscriptions == []

    def test_disabled_plugin_skips_backup(self, isolated_data_dir, monkeypatch):
        from core.db_maintenance import DBMaintenancePlugin, list_backups

        plugin = DBMaintenancePlugin()

        class _Bus:
            def subscribe(self, *a, **k):
                pass

            def unsubscribe(self, *a, **k):
                pass

        class _Ctx:
            config = {"agent": {}, "scheduler": {
                "db_maintenance": {"enabled": False}}}

            def __init__(self, bus):
                self.bus = bus

        asyncio.run(plugin.setup(_Ctx(_Bus())))

        class _Event:
            def get(self, key, default=None):
                return "db_backup" if key == "name" else default

        asyncio.run(plugin._on_cron(_Event()))
        assert list_backups(str(isolated_data_dir)) == []


class TestRestoreDB:
    def test_plaintext_restore_keeps_backup_aside(self, isolated_data_dir, tmp_path):
        _seed_db(isolated_data_dir)

        from core.backup_export import DataExporter, DataImporter
        archive = tmp_path / "backup.zip"
        assert DataExporter(data_dir=str(isolated_data_dir)).export_all(
            str(archive)).success

        # 目标环境已有库：还原后原库应被另存
        target = tmp_path / "target_env"
        target.mkdir()
        existing = target / "one_agent.db"
        existing.write_bytes(b"existing-old-db")

        res = DataImporter(data_dir=str(target)).import_from_file(
            str(archive), restore_db=True)
        assert res.success
        assert (target / "one_agent.db.pre_import").exists()
        # 新库是合法 SQLite（含 kv 数据）
        conn = sqlite3.connect(str(target / "one_agent.db"))
        assert conn.execute(
            "SELECT value FROM kv WHERE key='owner'").fetchone()[0] == "tester"
        conn.close()
