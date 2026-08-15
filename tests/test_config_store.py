"""v1.0.110 配置统一 + 记录持久化架构测试。

覆盖：
1. ConfigStore：种子导入 / 快照 / 增量更新 / 非法配置拒绝 / 0600 权限
2. overlay_enabled：项目 config/ 目录内 yaml 叠加，自建 yaml 完全接管
3. ApprovalManager：待办与历史跨实例持久化、过期待办重启后自动 deny
4. 微信凭据：旧 ~/.one-agent 目录搬迁 + JSON 文件一次性迁入 hub kv
"""

import json
import os
import stat
import time
from pathlib import Path

import pytest

from core.config_store import (
    ConfigStore,
    close_config_store,
    config_db_path,
    get_config_store,
    overlay_enabled,
    resolve_data_dir,
)
from core.hub import close_hub, get_hub


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """每个用例独立数据目录，并在结束时关闭单例连接。"""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(data_dir))
    yield data_dir
    close_config_store(str(config_db_path(str(data_dir))))
    close_hub()


# ============================================================
# 1. ConfigStore 基础行为
# ============================================================

class TestConfigStore:
    def test_seed_only_once(self, _isolated_data_dir):
        from one_agent import FullConfig
        store = get_config_store(str(config_db_path(str(_isolated_data_dir))))
        assert store.is_empty()
        assert store.seed_if_empty(FullConfig().model_dump()) is True
        # 二次种子是 no-op
        assert store.seed_if_empty({"agent": {"name": "X"}}) is False
        snap = store.snapshot()
        assert snap["agent"]["name"] != "X", "已有内容不得被种子覆盖"

    def test_apply_updates_merge(self, _isolated_data_dir):
        from one_agent import FullConfig
        store = get_config_store(str(config_db_path(str(_isolated_data_dir))))
        base = FullConfig().model_dump()
        store.apply_full(base)

        merged = store.apply_updates({"agent": {"language": "en"}})
        assert merged["agent"]["language"] == "en"
        # 其他字段保留
        assert merged["agent"]["name"] == base["agent"]["name"]
        # 快照可重放出同样结果
        assert store.snapshot()["agent"]["language"] == "en"

    def test_invalid_config_rejected(self, _isolated_data_dir):
        from one_agent import FullConfig
        store = get_config_store(str(config_db_path(str(_isolated_data_dir))))
        good = FullConfig().model_dump()
        store.apply_full(good)

        bad = dict(good)
        bad["llm"] = {**good["llm"], "retries": -5}  # ge=1 约束
        with pytest.raises(ValueError):
            store.apply_full(bad)
        # 库中保留原值
        assert store.snapshot()["llm"]["retries"] == good["llm"]["retries"]

    def test_db_file_mode_0600(self, _isolated_data_dir):
        from one_agent import FullConfig
        store = get_config_store(str(config_db_path(str(_isolated_data_dir))))
        store.apply_full(FullConfig().model_dump())
        db = config_db_path(str(_isolated_data_dir))
        assert stat.S_IMODE(db.stat().st_mode) == 0o600

    def test_corrupt_section_skipped(self, _isolated_data_dir):
        """损坏段不应让整个 snapshot 崩溃。"""
        store = get_config_store(str(config_db_path(str(_isolated_data_dir))))
        with store._conn:
            store._conn.execute(
                "INSERT INTO settings(section,payload,updated_at) VALUES('junk','{bad json',0)")
        assert store.snapshot() == {}


# ============================================================
# 2. overlay_enabled 路由规则
# ============================================================

class TestOverlayEnabled:
    def test_project_config_dir_overlays(self):
        root = Path(__file__).resolve().parent.parent / "config"
        assert overlay_enabled(str(root / "default_config.yaml")) is True
        assert overlay_enabled(str(root / "test_config.yaml")) is True

    def test_external_yaml_takes_over(self, tmp_path):
        # 自建 yaml（单测惯用法）→ 完全接管，不叠加 DB
        assert overlay_enabled(str(tmp_path / "config.yaml")) is False

    def test_none_path_enables(self):
        assert overlay_enabled(None) is True

    def test_resolve_data_dir_env_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(tmp_path))
        assert resolve_data_dir({"agent": {"data_dir": "./other"}}) == str(tmp_path)
        monkeypatch.delenv("ONE_AGENT_DATA_DIR")
        assert resolve_data_dir({"agent": {"data_dir": "./other"}}) == "./other"
        assert resolve_data_dir(None) == "./data"


# ============================================================
# 3. 审批持久化
# ============================================================

class TestApprovalPersistence:
    def _db(self, tmp_path):
        return str(tmp_path / "approvals.db")

    def test_pending_survives_restart(self, tmp_path):
        from core.approval import ApprovalManager
        db = self._db(tmp_path)
        m1 = ApprovalManager(db_path=db)
        req = m1.request_approval("rm -rf /tmp/x", "危险操作", risk_level="high")
        assert len(m1.get_pending()) == 1

        # 模拟重启：新实例同 DB
        m2 = ApprovalManager(db_path=db)
        pending = m2.get_pending()
        assert len(pending) == 1
        assert pending[0]["id"] == req.id
        assert pending[0]["operation"] == "rm -rf /tmp/x"
        assert pending[0]["risk_level"] == "high"

        # 原请求对象仍可被新实例审批（id 一致）
        assert m2.approve(req.id) is True
        assert m2.get_pending() == []

    def test_history_survives_restart(self, tmp_path):
        from core.approval import ApprovalManager
        db = self._db(tmp_path)
        m1 = ApprovalManager(db_path=db)
        r1 = m1.request_approval("op1", "d1")
        m1.approve(r1.id)
        r2 = m1.request_approval("op2", "d2")
        m1.deny(r2.id)

        m2 = ApprovalManager(db_path=db)
        hist = m2._history
        assert {h["id"] for h in hist} == {r1.id, r2.id}
        by_id = {h["id"]: h["approved"] for h in hist}
        assert by_id[r1.id] is True
        assert by_id[r2.id] is False

    def test_expired_pending_denied_on_restart(self, tmp_path, monkeypatch):
        from core.approval import ApprovalManager
        db = self._db(tmp_path)
        m1 = ApprovalManager(db_path=db)
        req = m1.request_approval("op", "d")

        # 篡改 created_at 为很久以前（模拟 TTL 过期后重启）
        with m1._conn:
            m1._conn.execute("UPDATE approvals SET created_at=?", (time.time() - 3600,))

        m2 = ApprovalManager(db_path=db)
        assert m2.get_pending() == []  # 过期待办不恢复
        assert any(h["id"] == req.id and not h["approved"] for h in m2._history)

    def test_memory_only_mode_when_db_fails(self, tmp_path):
        """DB 路径非法时降级纯内存，功能不受影响。"""
        from core.approval import ApprovalManager
        m = ApprovalManager(db_path=str(tmp_path / "no" / "perm" / "x" / "approvals.db"))
        # create_sqlite_connection 会自动建目录……用文件当目录的方式制造失败：
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        m2 = ApprovalManager(db_path=str(blocker / "sub" / "approvals.db"))
        assert m2._conn is None
        req = m2.request_approval("op", "d")
        assert m2.approve(req.id) is True


# ============================================================
# 4. 微信凭据：旧目录搬迁 + JSON → hub kv 一次性迁移
# ============================================================

class TestWechatDataDirMigration:
    def test_migrates_legacy_home_dir(self, tmp_path, monkeypatch):
        """旧 ~/.one-agent/weixin/accounts 先整体搬到 data 目录下（行为不变）。"""
        from gateways.wechat_personal import _resolve_data_dir
        monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(tmp_path / "data"))
        legacy = tmp_path / "legacy" / "accounts"
        legacy.mkdir(parents=True)
        (legacy / "acc1.json").write_text('{"account_id":"acc1"}', encoding="utf-8")

        resolved = _resolve_data_dir(legacy=legacy)

        new_dir = tmp_path / "data" / "gateways" / "weixin" / "accounts"
        assert resolved == new_dir
        assert (new_dir / "acc1.json").exists(), "旧凭据应被搬移到新目录"
        assert not legacy.exists(), "旧目录应已清空移除"

    def test_new_dir_used_when_no_legacy(self, tmp_path, monkeypatch):
        from gateways.wechat_personal import _resolve_data_dir
        monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(tmp_path / "data"))
        resolved = _resolve_data_dir(legacy=tmp_path / "not-exist")
        assert resolved == tmp_path / "data" / "gateways" / "weixin" / "accounts"

    def test_fallback_to_legacy_on_failure(self, tmp_path, monkeypatch):
        """搬迁失败（新位置被文件占位）时回退旧目录，不抛异常。"""
        from gateways.wechat_personal import _resolve_data_dir
        data_dir = tmp_path / "data"
        monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(data_dir))
        legacy = tmp_path / "legacy" / "accounts"
        legacy.mkdir(parents=True)
        # 让 new_dir 的父级是一个普通文件 → mkdir 失败 → 回退 legacy
        (tmp_path / "data").write_text("blocker", encoding="utf-8")
        resolved = _resolve_data_dir(legacy=legacy)
        assert resolved == legacy


class TestWechatCredentialsToKv:
    """v2.1.0：JSON 凭据文件一次性迁入 hub kv，目录改名 .migrated 保留兜底。"""

    def _make_accounts_dir(self, tmp_path):
        accounts = tmp_path / "data" / "gateways" / "weixin" / "accounts"
        accounts.mkdir(parents=True)
        return accounts

    def test_files_migrated_to_kv_and_dir_renamed(self, tmp_path, monkeypatch):
        import gateways.wechat_personal as wp
        accounts = self._make_accounts_dir(tmp_path)
        (accounts / "acc1.json").write_text(json.dumps({
            "account_id": "acc1",
            "token": "tok-1",
            "base_url": "https://ilinkai.weixin.qq.com",
            "user_id": "u1",
            "saved_at": "2026-07-03T05:39:09Z",
        }), encoding="utf-8")
        # 旧版凭据可能缺 account_id 字段 → 应从文件名推断
        (accounts / "c147268ca92c@im.bot.json").write_text(json.dumps({
            "token": "tok-legacy",
            "base_url": "https://ilinkai.weixin.qq.com",
            "user_id": "u2",
            "saved_at": "2026-07-04T05:39:09Z",
        }), encoding="utf-8")
        (accounts / "acc1.sync.json").write_text("sync-buf-abc", encoding="utf-8")
        monkeypatch.setattr(wp, "DATA_DIR", accounts)

        wp._ensure_migrated()

        hub = get_hub()
        acc1 = hub.kv_get("wechat.account.acc1")
        assert acc1 is not None and acc1["token"] == "tok-1"
        legacy_acc = hub.kv_get("wechat.account.c147268ca92c@im.bot")
        assert legacy_acc is not None and legacy_acc["token"] == "tok-legacy"
        assert legacy_acc["account_id"] == "c147268ca92c@im.bot", "缺 account_id 时从文件名推断"
        assert hub.kv_get("wechat.sync.acc1") == "sync-buf-abc"
        # 目录改名保留兜底，原目录消失
        assert not accounts.exists()
        migrated = accounts.with_name("accounts.migrated")
        assert migrated.is_dir()
        assert (migrated / "acc1.json").exists()

    def test_migration_skipped_when_kv_has_accounts(self, tmp_path, monkeypatch):
        """kv 已有账号（前次迁移/新登录写入）时不再动目录。"""
        import gateways.wechat_personal as wp
        accounts = self._make_accounts_dir(tmp_path)
        (accounts / "acc1.json").write_text('{"account_id":"acc1","token":"t"}', encoding="utf-8")
        monkeypatch.setattr(wp, "DATA_DIR", accounts)
        get_hub().kv_set("wechat.account.existing", {"account_id": "existing", "token": "t"})

        assert wp._ensure_migrated() is None  # 不抛异常
        assert accounts.exists(), "kv 已有账号时目录保持原样"
        assert get_hub().kv_get("wechat.account.acc1") is None

    def test_empty_dir_not_renamed(self, tmp_path, monkeypatch):
        """目录下无 JSON 文件时不迁移也不改名。"""
        import gateways.wechat_personal as wp
        accounts = self._make_accounts_dir(tmp_path)
        monkeypatch.setattr(wp, "DATA_DIR", accounts)
        wp._ensure_migrated()
        assert accounts.exists()
        assert not accounts.with_name("accounts.migrated").exists()

    def test_find_saved_account_reads_kv(self, tmp_path, monkeypatch):
        """_find_saved_account 走 kv（含 saved_at 最新者胜出）。"""
        import gateways.wechat_personal as wp
        from gateways.wechat_personal import WeChatPersonalGateway
        accounts = self._make_accounts_dir(tmp_path)
        monkeypatch.setattr(wp, "DATA_DIR", accounts)
        hub = get_hub()
        hub.kv_set("wechat.account.old", {
            "account_id": "old", "token": "t-old",
            "base_url": "https://x", "user_id": "u",
            "saved_at": "2026-07-01T00:00:00Z",
        })
        hub.kv_set("wechat.account.new", {
            "account_id": "new", "token": "t-new",
            "base_url": "https://x", "user_id": "u",
            "saved_at": "2026-07-05T00:00:00Z",
        })

        best = WeChatPersonalGateway()._find_saved_account()
        assert best is not None
        assert best["account_id"] == "new"
        assert best["token"] == "t-new"


class TestWechatSyncCursorKv:
    """同步游标读写走 hub kv（wechat.sync.<account_id>）。"""

    def test_sync_buf_roundtrip_via_kv(self, tmp_path, monkeypatch):
        import gateways.wechat_personal as wp
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "no-files")
        assert wp._load_sync_buf("acc9") == ""
        wp._save_sync_buf("acc9", "buf-xyz-123")
        assert wp._load_sync_buf("acc9") == "buf-xyz-123"
        assert get_hub().kv_get("wechat.sync.acc9") == "buf-xyz-123"
        assert not (tmp_path / "no-files").exists(), "游标不再落盘"

    def test_save_credentials_to_kv(self, tmp_path, monkeypatch):
        import gateways.wechat_personal as wp
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "no-files")
        wp._save_credentials("acc1", token="tok-123", base_url="http://b", user_id="u1")
        data = get_hub().kv_get("wechat.account.acc1")
        assert data is not None
        assert data["token"] == "tok-123"
        assert data["base_url"] == "http://b"
        assert data["user_id"] == "u1"
        assert data["account_id"] == "acc1"
        assert "saved_at" in data

    def test_delete_account_removes_kv_entries(self, tmp_path, monkeypatch):
        import gateways.wechat_personal as wp
        monkeypatch.setattr(wp, "DATA_DIR", tmp_path / "no-files")
        wp._save_credentials("acc1", token="tok-123", base_url="http://b", user_id="u1")
        wp._save_sync_buf("acc1", "buf-1")
        wp._delete_account("acc1")
        assert get_hub().kv_get("wechat.account.acc1") is None
        assert get_hub().kv_get("wechat.sync.acc1") is None
