"""Unit tests for MarketplacePlugin.

Covers:
  - MarketplacePlugin lifecycle (setup/start/stop)
  - SkillPackage class
  - v2.1.0 单一数据库：注册表→kv、技能包→stored_files、磁盘双写
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestMarketplaceLifecycle:
    """Test MarketplacePlugin setup, start, and stop."""

    def test_marketplace_import(self):
        """MarketplacePlugin imports successfully."""
        from marketplace import MarketplacePlugin, SkillPackage
        assert MarketplacePlugin is not None
        assert SkillPackage is not None
        assert MarketplacePlugin.name == "marketplace"

    def test_skill_package_init(self):
        """SkillPackage initializes with correct attributes."""
        from marketplace import SkillPackage

        pkg = SkillPackage(name="test-skill", version="1.0.0", description="A test", author="Test")
        assert pkg.name == "test-skill"
        assert pkg.version == "1.0.0"
        assert pkg.description == "A test"
        assert pkg.author == "Test"
        assert pkg.sha256 == ""
        assert pkg.installed_at is None
        assert pkg.tags == []

    def test_skill_package_from_directory_no_skill_md(self):
        """SkillPackage.from_directory returns None when SKILL.md missing."""
        from marketplace import SkillPackage

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg = SkillPackage.from_directory(tmpdir)
            assert pkg is None

    def test_skill_package_from_directory_with_skill_md(self):
        """SkillPackage.from_directory loads from valid directory."""
        from marketplace import SkillPackage

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_md = Path(tmpdir) / "SKILL.md"
            skill_md.write_text("""---
name: Test Skill
version: 2.0.0
description: A test skill
author: Author
---
# Test Skill
""")
            pkg = SkillPackage.from_directory(tmpdir)
            assert pkg is not None
            # Directory name is random, just check it's not empty
            assert pkg.name != ""
            assert pkg.version == "2.0.0"
            assert pkg.description == "A test skill"
            assert pkg.author == "Author"

    def test_skill_package_to_dict(self):
        """SkillPackage.to_dict returns correct dictionary."""
        from marketplace import SkillPackage

        pkg = SkillPackage(name="test", version="1.0.0", description="Test", author="Me")
        pkg.sha256 = "abc123"
        pkg.tags = ["tag1", "tag2"]

        d = pkg.to_dict()
        assert d["name"] == "test"
        assert d["version"] == "1.0.0"
        assert d["description"] == "Test"
        assert d["author"] == "Me"
        assert d["sha256"] == "abc123"
        assert d["tags"] == ["tag1", "tag2"]


# ============================================================
# v2.1.0 单一数据库统一：registry.json → kv、包目录 → stored_files
# ============================================================

def _make_skill_pkg(base: Path, name: str) -> Path:
    """Create a minimal skill package directory (SKILL.md + handler.py)."""
    src = base / name
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: 1.0.0\ndescription: test skill\nauthor: t\n---\n# {name}\n",
        encoding="utf-8")
    (src / "handler.py").write_text("def handle(args):\n    return 'ok'\n", encoding="utf-8")
    return src


class TestMarketplaceHubUnification:
    """Marketplace 存储进统一库 {data_dir}/one_agent.db，磁盘只是缓存。"""

    @pytest.fixture(autouse=True)
    def _isolated_data_dir(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("ONE_AGENT_DATA_DIR", str(data_dir))
        yield data_dir
        from core.hub import close_hub
        close_hub(str(data_dir))

    def test_migration_from_legacy_registry(self, tmp_path):
        """旧 registry.json + 包目录 → kv + stored_files，文件改名 .migrated。"""
        from core.hub import get_hub
        from marketplace import Marketplace

        mp_dir = self._mp_dir()
        (mp_dir / "registry.json").write_text(json.dumps({
            "updated_at": 1.0,
            "packages": [{"name": "legacy-skill", "version": "1.0.0",
                          "description": "d", "author": "a", "sha256": "", "tags": []}],
        }), encoding="utf-8")
        _make_skill_pkg(mp_dir, "legacy-skill")

        m = Marketplace(str(mp_dir))
        hub = get_hub(str(self.data_dir))

        reg = hub.kv_get("marketplace.registry")
        assert isinstance(reg, dict)
        assert reg["packages"][0]["name"] == "legacy-skill"
        # 包内路径相对包根
        assert "SKILL.md" in hub.files_get("marketplace/legacy-skill")
        assert "handler.py" in hub.files_get("marketplace/legacy-skill")
        assert not (mp_dir / "registry.json").exists()
        assert (mp_dir / "registry.json.migrated").exists()
        assert [p["name"] for p in m.discover()] == ["legacy-skill"]
        # 幂等：再次初始化不重复迁移、不丢数据
        Marketplace(str(mp_dir))
        assert hub.kv_get("marketplace.registry")["packages"][0]["name"] == "legacy-skill"

    def test_publish_install_uninstall_db_and_disk(self, tmp_path):
        """publish/install/uninstall 全程 DB+磁盘双写，DB 为准。"""
        from core.hub import get_hub
        from marketplace import Marketplace

        m = Marketplace(str(self._mp_dir()))
        hub = get_hub(str(self.data_dir))

        # publish：源目录 → DB（marketplace/<name>）+ 物化磁盘缓存
        src = _make_skill_pkg(tmp_path, "demo")
        pkg = m.publish(str(src))
        assert pkg is not None and pkg.sha256
        files = hub.files_get("marketplace/demo")
        assert "SKILL.md" in files and "handler.py" in files
        assert (self._mp_dir() / "demo" / "handler.py").is_file()

        # install：DB → skills/marketplace 磁盘 + 安装副本入库
        skills_dir = self.data_dir / "skills" / "marketplace"
        skills_dir.mkdir(parents=True)
        assert m.install("demo", str(skills_dir)) is True
        assert (skills_dir / "demo" / "handler.py").is_file()
        installed = hub.files_get("skills/marketplace")
        assert "demo/SKILL.md" in installed and "demo/handler.py" in installed
        assert m.list_installed(str(skills_dir)) == ["demo"]

        # uninstall：磁盘目录删除 + DB 同步（不残留、不复活）
        assert m.uninstall("demo", str(skills_dir)) is True
        assert not (skills_dir / "demo").exists()
        assert hub.files_get("skills/marketplace") == {}
        assert m.list_installed(str(skills_dir)) == []

        # 从市场目录移除已发布包 → files_delete(marketplace/<name>)
        assert m.uninstall("demo", str(self._mp_dir())) is True
        assert hub.files_get("marketplace/demo") == {}

    async def test_plugin_registry_kv_and_community_sync(self):
        """MarketplacePlugin 注册表走 kv；安装目录 skills/community 入库同步。"""
        from core.context import AgentContext
        from core.events import EventBus
        from core.hub import get_hub
        from marketplace import MarketplacePlugin, capture_skill_dir

        ctx = AgentContext(
            config={"agent": {"data_dir": str(self.data_dir)}, "marketplace": {}},
            bus=EventBus(),
        )
        p = MarketplacePlugin()
        await p.setup(ctx)
        hub = get_hub(str(self.data_dir))

        assert p._read_registry() == {"installed": [], "available": []}

        # 与 Marketplace 共用同一 kv dict：各自只更新自己的键
        hub.kv_set("marketplace.registry",
                   {"packages": [{"name": "x"}], "installed": [], "available": []})
        p._write_registry({"installed": [{"id": "s1"}], "available": []})
        blob = hub.kv_get("marketplace.registry")
        assert blob["packages"] == [{"name": "x"}]      # Marketplace 的键未丢
        assert blob["installed"] == [{"id": "s1"}]      # plugin 的键已写入

        # 已安装的 .md 文件入库；uninstall 后磁盘+DB 同步删除
        install_dir = self.data_dir / "skills" / "community"
        f = install_dir / "s1.md"
        f.write_text("---\nid: s1\ntitle: S1\ndescription: d\n---\nbody", encoding="utf-8")
        capture_skill_dir(hub, install_dir)
        assert "s1.md" in hub.files_get("skills/community")

        assert (await p.uninstall("s1"))["ok"] is True
        assert not f.exists()
        assert hub.files_get("skills/community") == {}
        assert p.list_installed() == []
        await p.stop()

    async def test_skills_dir_sync_and_restart_marker_kv(self, monkeypatch):
        """SkillManager.setup：先物化 DB→磁盘再采集磁盘→DB；restart 标记写 kv。"""
        from core.context import AgentContext
        from core.events import EventBus
        from core.hub import get_hub
        from skills import SkillManager

        ctx = AgentContext(config={"agent": {"data_dir": str(self.data_dir)}},
                           bus=EventBus())
        hub = get_hub(str(self.data_dir))

        # 磁盘存量技能（用户手写在 skills/user）→ 首次启动采集入库
        user_dir = self.data_dir / "skills" / "user"
        (user_dir / "hello").mkdir(parents=True)
        (user_dir / "hello" / "SKILL.md").write_text(
            "---\nid: hello-skill\ntitle: Hello\ndescription: say hi\n---\nhi",
            encoding="utf-8")
        # DB-only 技能（skills/marketplace）→ 物化到磁盘
        hub.files_put("skills/marketplace", {
            "bye/SKILL.md": b"---\nid: bye-skill\ntitle: Bye\ndescription: say bye\n---\nbye",
        })

        sm = SkillManager()
        await sm.setup(ctx)

        assert "hello-skill" in sm.all_skill_ids()
        assert "bye-skill" in sm.all_skill_ids()
        assert (user_dir / "hello" / "SKILL.md").is_file()
        assert (self.data_dir / "skills" / "marketplace" / "bye" / "SKILL.md").is_file()
        assert "hello/SKILL.md" in hub.files_get("skills/user")
        assert "bye/SKILL.md" in hub.files_get("skills/marketplace")

        # restart 标记 → 统一库 kv（不再写 restart_marker.json）
        execv_calls = []
        monkeypatch.setattr(os, "execv", lambda *a, **k: execv_calls.append(a))
        restart = sm.get("restart")
        assert restart is not None
        await restart.handler({})
        marker = get_hub().kv_get("restart_marker")
        assert isinstance(marker, dict) and "timestamp" in marker
        assert not (self.data_dir / "restart_marker.json").exists()
        # 取消延迟重启任务，防止测试结束后触发 execv
        task = getattr(sm, "_restart_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------ helpers
    @property
    def data_dir(self) -> Path:
        return Path(os.environ["ONE_AGENT_DATA_DIR"])

    def _mp_dir(self) -> Path:
        mp = self.data_dir / "marketplace"
        mp.mkdir(parents=True, exist_ok=True)
        return mp