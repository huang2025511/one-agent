"""统一数据库架构（v2.1.0）集成测试。

验证核心承诺：
1. 所有系统状态（配置/会话/记忆/审批/成本/凭据/技能包）都在
   {data_dir}/one_agent.db 一个文件里
2. 旧版分散库自动迁移（含 FTS5、rowid 关联、schema 版本）
3. "复制一个数据库即可部署"：拷贝 one_agent.db 到新环境 →
   配置、聊天记录、技能包全部就位
"""
import json
import os
import shutil
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    dd = tmp_path / "data"
    dd.mkdir()
    monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(dd))
    yield dd
    from core.hub import close_hub
    close_hub()
    from core.config_store import close_config_store
    close_config_store()


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _make_legacy_sessions(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, created_at REAL,
        updated_at REAL, message_count INTEGER, total_tokens INTEGER,
        status TEXT, parent_id TEXT, fork_point INTEGER);
    CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
        content TEXT, meta TEXT DEFAULT '{}', created_at REAL, tokens INTEGER);
    INSERT INTO sessions VALUES ('s1', '旧会话', 1, 1, 2, 10, 'active', NULL, NULL);
    INSERT INTO messages VALUES ('m1', 's1', 'user', '你好', '{}', 1, 5);
    INSERT INTO messages VALUES ('m2', 's1', 'assistant', '你好！', '{}', 2, 5);
    """)
    c.execute("PRAGMA user_version = 2")
    c.commit()
    c.close()


class TestUnifiedDatabase:
    def test_all_stores_share_one_db(self, isolated_data_dir):
        """各 store 打开的是同一个 one_agent.db 文件，表名互不冲突。"""
        from core.hub import database_path
        db = database_path(str(isolated_data_dir))

        from core.config_store import get_config_store
        from memory.session_store import SessionStore
        store = SessionStore(db)
        store.create_session("s", "t")
        store.add_message("s", "user", "hi")
        get_config_store(db).seed_if_empty({"agent": {"name": "T"}})

        tables = _tables(db)
        assert "sessions" in tables and "messages" in tables
        # 核心中枢表（settings 由 ConfigStore 创建）
        assert {"settings", "kv", "stored_files", "schema_versions",
                "config_backups"} <= tables
        store.close()

    def test_schema_version_per_store(self, isolated_data_dir):
        """多个 BaseSQLiteStore 共库时 schema 版本互不干扰。"""
        from core.hub import database_path
        from memory.knowledge_graph import KnowledgeGraph
        from memory.session_store import SessionStore
        db = database_path(str(isolated_data_dir))

        s = SessionStore(db)   # SCHEMA_VERSION = 2
        k = KnowledgeGraph(db)  # SCHEMA_VERSION = 1
        conn = sqlite3.connect(db)
        try:
            versions = dict(conn.execute(
                "SELECT store, version FROM schema_versions"))
        finally:
            conn.close()
        assert versions["SessionStore"] == 2
        assert versions["KnowledgeGraph"] == 1
        s.close()
        k.close()

    def test_db_file_mode_0600(self, isolated_data_dir):
        from core.hub import get_hub
        hub = get_hub(str(isolated_data_dir))
        import stat
        assert stat.S_IMODE(Path(hub.path).stat().st_mode) == 0o600


class TestLegacyMigration:
    def test_sessions_migrated_with_data_and_version(self, isolated_data_dir):
        from core.hub import database_path, migrate_legacy
        _make_legacy_sessions(isolated_data_dir / "memory" / "sessions.db")

        report = migrate_legacy(str(isolated_data_dir))
        assert any("sessions.db" in r for r in report)

        db = database_path(str(isolated_data_dir))
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
            assert conn.execute(
                "SELECT title FROM sessions WHERE id='s1'").fetchone()[0] == "旧会话"
            # 旧库 user_version=2 继承到 schema_versions，避免重跑迁移
            assert dict(conn.execute(
                "SELECT store, version FROM schema_versions"))["SessionStore"] == 2
        finally:
            conn.close()
        # 原文件退役为 .migrated
        assert (isolated_data_dir / "memory" / "sessions.db.migrated").exists()

    def test_fts5_memory_with_rowid_linked_weights(self, isolated_data_dir):
        from core.hub import database_path, migrate_legacy
        legacy = isolated_data_dir / "memory" / "longterm.sqlite"
        legacy.parent.mkdir(parents=True)
        c = sqlite3.connect(legacy)
        c.executescript(
            "CREATE VIRTUAL TABLE memory USING fts5(content, source, tags, timestamp UNINDEXED);"
            "CREATE TABLE memory_weights (rowid INTEGER PRIMARY KEY, weight REAL DEFAULT 1.0);")
        c.execute("INSERT INTO memory(content, source, tags, timestamp) "
                  "VALUES ('重要记忆', 'user', '', 1)")
        rid = c.execute("SELECT rowid FROM memory").fetchone()[0]
        c.execute("INSERT INTO memory_weights VALUES (?, 1.5)", (rid,))
        c.commit()
        c.close()

        migrate_legacy(str(isolated_data_dir))
        conn = sqlite3.connect(database_path(str(isolated_data_dir)))
        try:
            # FTS 可检索
            assert conn.execute(
                "SELECT count(*) FROM memory WHERE memory MATCH '重要记忆'"
            ).fetchone()[0] == 1
            # rowid 关联不断裂（memory_weights ↔ memory）
            w = conn.execute(
                "SELECT w.weight FROM memory_weights w "
                "JOIN memory m ON m.rowid = w.rowid").fetchone()
            assert w and w[0] == 1.5
        finally:
            conn.close()

    def test_idempotent_and_non_empty_target_skipped(self, isolated_data_dir):
        from core.hub import migrate_legacy
        _make_legacy_sessions(isolated_data_dir / "memory" / "sessions.db")
        assert migrate_legacy(str(isolated_data_dir))
        # 二次迁移：文件已改名 → 空报告
        assert migrate_legacy(str(isolated_data_dir)) == []


class TestCopyOneFileDeployment:
    def test_copy_db_to_new_env_restores_everything(self, isolated_data_dir, tmp_path, monkeypatch):
        """端到端：配置 + 聊天记录 + 技能包 → 拷贝单文件 → 新环境全部就位。"""
        from core.hub import database_path, get_hub

        # 环境 A：写配置、会话、技能包
        hub = get_hub(str(isolated_data_dir))
        hub.kv_set("wechat.account.wxid_1", {"token": "t", "user_id": "u1"})
        hub.files_put("skills/user/my_tool", {"SKILL.md": "# 我的技能", "handler.py": "x=1"})
        from memory.session_store import SessionStore
        store = SessionStore(database_path(str(isolated_data_dir)))
        store.create_session("deploy-test", "迁移会话")
        store.add_message("deploy-test", "user", "要迁移的消息")
        store.close()
        from core.config_store import close_config_store
        close_config_store()
        hub.checkpoint()

        # 模拟部署新环境：仅拷贝 one_agent.db 一个文件
        new_dir = tmp_path / "new-env-data"
        new_dir.mkdir()
        shutil.copy2(database_path(str(isolated_data_dir)),
                     new_dir / "one_agent.db")

        monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(new_dir))
        hub2 = get_hub(str(new_dir))
        assert hub2.kv_get("wechat.account.wxid_1")["token"] == "t"
        assert hub2.files_get("skills/user/my_tool")["SKILL.md"] == b"# \xe6\x88\x91\xe7\x9a\x84\xe6\x8a\x80\xe8\x83\xbd"
        hub2.materialize("skills/user/my_tool", new_dir / "skills" / "user" / "my_tool")
        assert (new_dir / "skills" / "user" / "my_tool" / "handler.py").read_text() == "x=1"

        store2 = SessionStore(str(new_dir / "one_agent.db"))
        got = store2.get_session("deploy-test")
        assert got is not None
        assert got["messages"][0]["content"] == "要迁移的消息"
        store2.close()


class TestConcurrentAccess:
    def test_wal_multi_writer_under_threads(self, isolated_data_dir):
        """WAL 多连接并发写不丢数据（统一库的关键前提）。"""
        from core.hub import database_path
        db = database_path(str(isolated_data_dir))
        from memory.session_store import SessionStore
        store = SessionStore(db)
        errors = []

        def writer(n):
            try:
                s = SessionStore(db)
                for i in range(10):
                    s.add_message(f"sess-{n}", "user", f"msg-{n}-{i}")
                s.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"并发写失败: {errors}"
        total = 0
        for n in range(4):
            total += len(store.get_session(f"sess-{n}")["messages"])
        assert total == 40
        store.close()


class TestBackupExportImport:
    """v2.1.0：备份导出/导入走统一库（不再碰旧分散库与 YAML）。"""

    def _seed(self, data_dir):
        from core.hub import database_path
        db = database_path(str(data_dir))
        from memory.session_store import SessionStore
        s = SessionStore(db)
        s.create_session("bx-1", "备份会话")
        s.add_message("bx-1", "user", "备份内容")
        s.close()
        conn = sqlite3.connect(db)
        # FTS memory 表由 LongTermMemory 建；这里直接补建最小 FTS 表
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory USING fts5"
            "(content, source, tags, timestamp UNINDEXED)")
        conn.execute("INSERT INTO memory(content, source, tags, timestamp) "
                     "VALUES ('备份记忆', 'user', '', 9)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entities (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL UNIQUE, type TEXT DEFAULT 'unknown', source TEXT DEFAULT '',"
            " created_at REAL, updated_at REAL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS relations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " subject_id INTEGER NOT NULL, predicate TEXT NOT NULL, object_id INTEGER NOT NULL,"
            " weight REAL DEFAULT 1.0, source TEXT DEFAULT '', created_at REAL)")
        conn.execute("INSERT INTO entities(name, type) VALUES ('张三', 'person')")
        conn.execute("INSERT INTO entities(name, type) VALUES ('项目A', 'project')")
        ids = dict(conn.execute("SELECT name, id FROM entities"))
        conn.execute("INSERT INTO relations(subject_id, predicate, object_id) VALUES (?, ?, ?)",
                     (ids["张三"], "参与", ids["项目A"]))
        conn.commit()
        conn.close()
        from core.config_store import close_config_store, get_config_store
        get_config_store(db).seed_if_empty({"agent": {"name": "BackupAgent"}})
        close_config_store(db)
        return db

    def test_zip_roundtrip_across_envs(self, isolated_data_dir, tmp_path, monkeypatch):
        from core.backup_export import DataExporter, DataImporter
        self._seed(isolated_data_dir)

        archive = tmp_path / "backup.zip"
        result = DataExporter(data_dir=str(isolated_data_dir)).export_all(str(archive))
        assert result.success, result.error
        import zipfile
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            assert "one_agent.db" in names, "zip 应附统一库原文件（可直接部署）"
            sessions = json.loads(zf.read("sessions.json"))
            assert sessions["session_count"] == 1

        # 新环境导入（merge 模式）
        new_dir = tmp_path / "new-data"
        new_dir.mkdir()
        monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(new_dir))
        # 统一库需已存在（业务表结构就位）
        from memory.session_store import SessionStore
        s = SessionStore(str(new_dir / "one_agent.db"))
        s.create_session("placeholder", "占位")
        s.close()
        from core.hub import close_hub
        close_hub()

        r = DataImporter(data_dir=str(new_dir)).import_from_file(str(archive), merge=True)
        assert r.success, r.error
        assert r.items_imported.get("sessions") == 1
        conn = sqlite3.connect(str(new_dir / "one_agent.db"))
        try:
            assert conn.execute(
                "SELECT count(*) FROM sessions WHERE id='bx-1'").fetchone()[0] == 1
            assert conn.execute(
                "SELECT count(*) FROM memory WHERE memory MATCH '备份记忆'").fetchone()[0] >= 1
            n = conn.execute(
                "SELECT count(*) FROM relations r JOIN entities s ON s.id=r.subject_id "
                "JOIN entities o ON o.id=r.object_id "
                "WHERE s.name='张三' AND o.name='项目A'").fetchone()[0]
            assert n >= 1, "kg 关系应按名称解析导入"
        finally:
            conn.close()

    def test_config_import_writes_db_not_yaml(self, isolated_data_dir, tmp_path):
        from core.backup_export import DataImporter
        from core.hub import database_path
        self._seed(isolated_data_dir)
        # 导入到已有统一库：merge 更新生效且不触碰任何 yaml
        yaml_probe = tmp_path / "probe.yaml"
        yaml_probe.write_text("x: 1", encoding="utf-8")
        # 直接走内部方法（zip/json 打包格式之外的等价路径）
        imp = DataImporter(data_dir=str(isolated_data_dir))
        assert imp._import_config({"agent": {"language": "zh"}}, merge=True) == 1
        assert imp._import_config({"agent": {"name": "Restored"}}, merge=False) == 1
        from core.config_store import close_config_store, get_config_store
        db = database_path(str(isolated_data_dir))
        snap = get_config_store(db).snapshot()
        close_config_store(db)
        assert snap["agent"]["name"] == "Restored"
        assert yaml_probe.read_text(encoding="utf-8") == "x: 1"


class TestCustomProvidersMigration:
    def test_legacy_file_migrated_to_kv(self, isolated_data_dir, tmp_path, monkeypatch):
        """scripts/fetch_models 自定义 provider：home 旧文件 → 统一库 kv。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fetch_models", str(Path(__file__).parent.parent / "scripts" / "fetch_models.py"))
        fm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fm)

        legacy = tmp_path / "custom_providers.json"
        legacy.write_text(json.dumps(
            {"my-relay": {"name": "My Relay", "base_url": "http://x", "api_key": "k"}}),
            encoding="utf-8")
        monkeypatch.setattr(fm, "_LEGACY_PROVIDERS_FILE", legacy)

        providers = fm.load_custom_providers()
        assert providers["my-relay"]["base_url"] == "http://x"
        # 旧文件退役 + 值在 kv
        assert legacy.with_name(legacy.name + ".migrated").exists()
        from core.hub import get_hub
        assert get_hub().kv_get("llm.custom_providers")["my-relay"]["api_key"] == "k"

        # 保存走 kv，不再新建任何文件
        fm.save_custom_provider("Another", "http://y")
        again = fm.load_custom_providers()
        assert "another" in again and "my-relay" in again


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
