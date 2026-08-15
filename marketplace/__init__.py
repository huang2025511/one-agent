"""Skill marketplace — publish, discover, and install skill packages.

A skill package is a directory containing:
  - SKILL.md (required): skill metadata and documentation
  - handler.py (optional): Python handler function
  - references/ (optional): reference documents
  - scripts/ (optional): helper scripts

v2.1.0 单一数据库统一：注册表与技能包文件全部收拢进统一库
{data_dir}/one_agent.db（Hub）：
  - registry.json             → kv["marketplace.registry"]
  - marketplace/<name>/ 包目录 → stored_files 包 "marketplace/<name>"
磁盘目录只是运行时缓存（handler.py 需落盘才能被 import），
由 Hub.materialize 物化、capture_dir 回写 DB。
旧版 registry.json 首次启动一次性迁移为 registry.json.migrated 保留兜底。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import hashlib
import time
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx

from core.hub import Hub, get_hub
from core.plugin import Plugin

logger = logging.getLogger(__name__)

# Official community hub base URL
COMMUNITY_HUB_URL = "https://raw.githubusercontent.com/huang2025511/one-agent-skills/main/"

# 注册表在统一库 kv 中的 key（Marketplace 与 MarketplacePlugin 共用同一个
# dict 值，各自只更新自己的键，避免互相覆盖：
#   Marketplace       → "updated_at" + "packages"
#   MarketplacePlugin → "installed"  + "available"
_REGISTRY_KV_KEY = "marketplace.registry"

# 技能目录（{data_dir}/skills/<kind>）在 stored_files 中的包名约定
_SKILL_KINDS = ("user", "community", "marketplace")


def _marketplace_pkg(name: str) -> str:
    """市场已发布技能包 → stored_files 包名。"""
    return f"marketplace/{name}"


def _skill_dir_pkg(target_dir: Union[str, Path]) -> Optional[str]:
    """技能目录 → stored_files 包名（skills/<kind>）；非技能目录返回 None。"""
    p = Path(target_dir)
    if p.parent.name == "skills" and p.name in _SKILL_KINDS:
        return f"skills/{p.name}"
    return None


def capture_skill_dir(hub: Hub, target_dir: Union[str, Path]) -> int:
    """技能目录 → DB 整包采集（仅对 {data_dir}/skills/<kind> 生效）。

    与 Hub.capture_dir 的差异：目录存在但无有效文件时显式清空该包，
    否则 uninstall 删掉最后一个文件后 DB 仍留旧行，下次物化会"复活"。
    """
    package = _skill_dir_pkg(target_dir)
    if package is None:
        return 0
    root = Path(target_dir)
    if not root.is_dir():
        return 0
    n = hub.capture_dir(package, root)
    if n == 0:
        hub.files_put(package, {})
    return n


def migrate_registry(registry_dir: Union[str, Path], hub: Hub) -> bool:
    """一次性迁移：registry.json → kv、已发布包目录 → stored_files。

    幂等：kv 已有注册表时直接返回 False（已迁移/新库已初始化）。
    迁移完成后 registry.json 改名为 registry.json.migrated 保留兜底。
    """
    root = Path(registry_dir)
    legacy = root / "registry.json"
    if hub.kv_get(_REGISTRY_KV_KEY) is not None:
        return False
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse legacy marketplace registry: %s", exc)
            data = {}
        hub.kv_set(_REGISTRY_KV_KEY, data if isinstance(data, dict) else {})
    captured: List[str] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file():
                if hub.capture_dir(_marketplace_pkg(child.name), child) > 0:
                    captured.append(child.name)
    if legacy.exists():
        try:
            legacy.rename(legacy.with_name("registry.json.migrated"))
        except OSError as exc:
            logger.warning("legacy registry rename failed: %s", exc)
    if captured:
        logger.info("marketplace migration: captured %d packages: %s",
                    len(captured), ", ".join(captured))
    return True


class SkillPackage:
    """Represents a skill package."""
    
    def __init__(self, name: str, version: str = "1.0.0", description: str = "",
                 author: str = "", path: str = ""):
        self.name = name
        self.version = version
        self.description = description
        self.author = author
        self.path = path
        self.sha256 = ""
        self.installed_at: Optional[float] = None
        self.tags: List[str] = []
    
    @classmethod
    def from_directory(cls, dirpath: str) -> Optional["SkillPackage"]:
        """Load a skill package from a directory."""
        path = Path(dirpath)
        skill_md = path / "SKILL.md"
        if not skill_md.exists():
            return None
        
        # Parse SKILL.md front matter
        content = skill_md.read_text(encoding='utf-8', errors='ignore')
        meta = cls._parse_front_matter(content)
        
        pkg = cls(
            name=path.name,
            version=meta.get("version", "1.0.0"),
            description=meta.get("description", ""),
            author=meta.get("author", ""),
            path=str(path),
        )
        return pkg
    
    @staticmethod
    def _parse_front_matter(content: str) -> Dict[str, str]:
        """Extract YAML front matter from SKILL.md."""
        lines = content.split("\n")
        if lines and lines[0].strip() == "---":
            meta = {}
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip()] = val.strip().strip('"').strip("'")
            return meta
        return {}
    
    def compute_hash(self) -> str:
        """Compute SHA256 of the skill package contents."""
        hasher = hashlib.sha256()
        path = Path(self.path)
        for f in sorted(path.rglob("*")):
            if f.is_file() and f.suffix != '.pyc':
                hasher.update(str(f.relative_to(path)).encode())
                hasher.update(f.read_bytes())
        self.sha256 = hasher.hexdigest()[:16]
        return self.sha256
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "sha256": self.sha256,
            "tags": self.tags,
        }


class Marketplace:
    """Skill marketplace for discovering and installing skill packages.

    存储分层（v2.1.0 单一数据库）：
      - 注册表          → Hub kv["marketplace.registry"]（单一事实源）
      - 已发布技能包    → Hub stored_files 包 "marketplace/<name>"
      - 磁盘 marketplace/ 目录 → 运行时缓存（materialize 物化）
    """

    def __init__(self, registry_dir: str = "data/marketplace"):
        self._registry_dir = Path(registry_dir)
        self._registry_dir.mkdir(parents=True, exist_ok=True)
        # registry_dir = {data_dir}/marketplace → 统一库跟随 data_dir
        self._hub: Hub = get_hub(str(self._registry_dir.parent))
        # 一次性迁移：旧 registry.json + 包目录 → 统一库（幂等）
        migrate_registry(self._registry_dir, self._hub)
        self._packages: Dict[str, SkillPackage] = {}
        self._load_registry()

    def _load_registry(self):
        data = self._hub.kv_get(_REGISTRY_KV_KEY)
        if not isinstance(data, dict):
            return
        try:
            for entry in data.get("packages", []) or []:
                pkg = SkillPackage(
                    name=entry["name"],
                    version=entry.get("version", "1.0.0"),
                    description=entry.get("description", ""),
                    author=entry.get("author", ""),
                )
                pkg.sha256 = entry.get("sha256", "")
                pkg.tags = entry.get("tags", [])
                self._packages[pkg.name] = pkg
        except Exception as exc:
            logger.warning("Failed to load marketplace registry: %s", exc)

    def _save_registry(self):
        # 只更新自己的键，保留 MarketplacePlugin 写入的 installed/available
        blob = self._hub.kv_get(_REGISTRY_KV_KEY)
        blob = blob if isinstance(blob, dict) else {}
        blob["updated_at"] = time.time()
        blob["packages"] = [p.to_dict() for p in self._packages.values()]
        self._hub.kv_set(_REGISTRY_KV_KEY, blob)

    def publish(self, dirpath: str) -> Optional[SkillPackage]:
        """Publish a skill package from a local directory."""
        pkg = SkillPackage.from_directory(dirpath)
        if pkg is None:
            return None
        pkg.compute_hash()

        # 磁盘 → DB（整包写入，DB 为单一事实源）
        if self._hub.capture_dir(_marketplace_pkg(pkg.name), dirpath) == 0:
            logger.warning("publish: no files captured from %s", dirpath)
            return None
        # DB → 磁盘（物化运行时缓存，替换旧副本）
        dest = self._registry_dir / pkg.name
        if dest.exists():
            shutil.rmtree(dest)
        self._hub.materialize(_marketplace_pkg(pkg.name), dest)
        pkg.path = str(dest)

        self._packages[pkg.name] = pkg
        self._save_registry()
        logger.info("Published skill: %s v%s", pkg.name, pkg.version)
        return pkg

    def discover(self, query: str = "") -> List[Dict[str, Any]]:
        """Search available packages (DB 注册表为准)."""
        results = []
        for pkg in self._packages.values():
            if query and query.lower() not in pkg.name.lower() and query.lower() not in pkg.description.lower():
                continue
            results.append(pkg.to_dict())
        return sorted(results, key=lambda p: p["name"])

    def install(self, name: str, target_dir: str) -> bool:
        """Install a skill package to a target directory (e.g., ./skills/).

        DB 为源：物化 stored_files 包到目标目录（磁盘只是运行时缓存，
        handler.py 要能被 import）；目标为 skills/<kind> 目录时整目录
        重新采集回 DB，保证安装副本也入库。
        """
        if name not in self._packages:
            return False
        pkg = self._packages[name]
        if not self._hub.files_get(_marketplace_pkg(name)):
            # DB 缺失时从磁盘缓存兜底采集（正常流程 publish 已入库）
            src = self._registry_dir / pkg.name
            if not src.is_dir() or self._hub.capture_dir(_marketplace_pkg(name), src) == 0:
                return False
        dest = Path(target_dir) / name
        if dest.exists():
            shutil.rmtree(dest)
        if self._hub.materialize(_marketplace_pkg(name), dest) == 0:
            return False
        # 安装副本入库：目标目录是 {data_dir}/skills/<kind> 时整目录重采集
        capture_skill_dir(self._hub, target_dir)
        logger.info("Installed skill: %s → %s", name, dest)
        return True

    def uninstall(self, name: str, target_dir: str) -> bool:
        """Remove an installed skill package（磁盘目录 + DB 同步删除）."""
        dest = Path(target_dir) / name
        if not dest.exists():
            return False
        shutil.rmtree(dest)
        try:
            if dest.parent.resolve() == self._registry_dir.resolve():
                # 从市场移除已发布包 → 删除对应 stored_files 包
                self._hub.files_delete(_marketplace_pkg(name))
            else:
                # 安装副本删除 → 整目录重采集（capture 为整包替换，删除即同步）
                capture_skill_dir(self._hub, target_dir)
        except Exception as exc:
            logger.warning("uninstall: db sync failed for %s: %s", name, exc)
        logger.info("Uninstalled skill: %s", name)
        return True

    def list_installed(self, target_dir: str) -> List[str]:
        """List installed skill packages (DB 为准，磁盘兜底)."""
        package = _skill_dir_pkg(target_dir)
        if package is not None:
            files = self._hub.files_get(package)
            names = sorted({p.split("/", 1)[0] for p in files
                            if p.count("/") == 1 and p.endswith("/SKILL.md")})
            if names or not Path(target_dir).is_dir():
                return names
        path = Path(target_dir)
        if not path.exists():
            return []
        return [d.name for d in path.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]


# ============================================================
# Backward-compatible plugin wrapper (used by tests & one_agent.py)
# ============================================================

class SkillSpec:
    """Validated skill specification parsed from a markdown file."""

    def __init__(
        self,
        id: str,
        title: str,
        description: str,
        version: str,
        author: str,
        tags: List[str],
        raw_url: str,
        checksum: str,
    ) -> None:
        self.id = id
        self.title = title
        self.description = description
        self.version = version
        self.author = author
        self.tags = tags
        self.raw_url = raw_url
        self.checksum = checksum

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "tags": self.tags,
            "raw_url": self.raw_url,
            "checksum": self.checksum,
        }


class MarketplacePlugin(Plugin):
    """Skill marketplace: discover, verify, install, uninstall skills."""

    name = "marketplace"
    depends_on = ["skills"]

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        self._hub: Optional[Hub] = None
        self._install_dir: Optional[str] = None
        self._skills_plugin = None
        self._timeout = 30
        self._community_hub = COMMUNITY_HUB_URL

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("marketplace") or {}
        data_dir = ctx.config.get("agent", {}).get("data_dir", "./data")
        self._install_dir = os.path.join(data_dir, "skills", "community")
        self._community_hub = cfg.get("community_hub", COMMUNITY_HUB_URL)
        # 注册表/技能文件统一存 {data_dir}/one_agent.db（Hub）
        self._hub = get_hub(data_dir)
        Path(self._install_dir).mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(timeout=self._timeout)
        # 一次性迁移旧 registry.json（幂等；Marketplace 初始化通常已做过）
        migrate_registry(os.path.join(data_dir, "marketplace"), self._hub)
        self._ensure_registry()
        logger.info("marketplace ready, registry=hub kv %s", _REGISTRY_KV_KEY)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
        await super().stop()

    def _ensure_registry(self) -> None:
        if self._hub.kv_get(_REGISTRY_KV_KEY) is None:
            self._hub.kv_set(_REGISTRY_KV_KEY, {"installed": [], "available": []})

    def _read_registry(self) -> Dict[str, Any]:
        data = self._hub.kv_get(_REGISTRY_KV_KEY)
        if not isinstance(data, dict):
            return {"installed": [], "available": []}
        return {
            "installed": data.get("installed", []) or [],
            "available": data.get("available", []) or [],
        }

    def _write_registry(self, data: Dict[str, Any]) -> None:
        # 只更新自己的键，保留 Marketplace 写入的 packages/updated_at
        blob = self._hub.kv_get(_REGISTRY_KV_KEY)
        blob = blob if isinstance(blob, dict) else {}
        blob["installed"] = data.get("installed", []) or []
        blob["available"] = data.get("available", []) or []
        self._hub.kv_set(_REGISTRY_KV_KEY, blob)

    def _sync_install_dir(self) -> None:
        """安装目录（skills/community）整目录采集回 DB，保持 DB 为超集。"""
        try:
            capture_skill_dir(self._hub, self._install_dir)  # type: ignore[arg-type]
        except Exception as exc:
            logger.debug("sync skills/community to hub failed: %s", exc)

    # --------------------------------------------------------- public API
    async def install(self, source: str) -> Dict[str, Any]:
        """Install a skill from:
          - GitHub: "owner/repo[@tag]/path"  (e.g. "octocat/Hello-World/readme.md")
          - URL: full raw URL to a .md file
        Returns {"ok": True, "skill": {...}} or {"ok": False, "error": "..."}
        """
        # 安全提示：第三方技能可能包含可执行代码（shell 命令、Python 代码等）
        # 仅安装来自可信来源的技能
        logger.warning(
            "marketplace: installing skill from %s — "
            "第三方技能可能包含可执行代码，请确保来源可信！",
            source,
        )

        url = self._resolve_source(source)
        if not url:
            return {"ok": False, "error": f"invalid source: {source}"}

        # Fetch and validate
        try:
            resp = await self._client.get(url)  # type: ignore[union-attr]
            resp.raise_for_status()
            content = resp.text
            # prevent DoS: reject unreasonably large skill files (> 1 MB)
            if len(content) > 1024 * 1024:
                return {"ok": False, "error": f"skill too large ({len(content)} bytes, max 1 MB)"}
        except Exception as exc:
            return {"ok": False, "error": f"failed to fetch {url}: {exc}"}

        # Compute checksum
        checksum = hashlib.sha256(content.encode()).hexdigest()[:16]

        # Parse skill spec from front-matter
        spec = self._parse_front_matter(content, source)
        if spec is None:
            return {"ok": False, "error": "invalid skill format: missing YAML front-matter"}

        # Basic sanity: reject content that looks like non-markdown binary
        if len(content) < 10:
            return {"ok": False, "error": "content too short to be a valid skill"}

        spec.checksum = checksum
        spec.raw_url = url

        # Write to install dir
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", spec.id)
        dest = Path(self._install_dir) / f"{safe_id}.md"  # type: ignore[arg-type]
        dest.write_text(content, encoding="utf-8")

        # Update registry
        reg = self._read_registry()
        # Remove old version if present
        reg["installed"] = [s for s in reg.get("installed", []) if s.get("id") != spec.id]
        reg["installed"].append(spec.to_dict())
        self._write_registry(reg)
        # 安装副本入库：skills/community 整目录采集回 DB
        self._sync_install_dir()

        # Reload skill into SkillManager
        if self._skills_plugin is not None:
            self._skills_plugin._scan_directory(self._install_dir)  # type: ignore[union-attr]

        logger.info("installed skill %s from %s", spec.id, source)
        return {"ok": True, "skill": spec.to_dict()}

    async def uninstall(self, skill_id: str) -> Dict[str, Any]:
        """Remove an installed skill by id."""
        reg = self._read_registry()
        before = len(reg["installed"])
        reg["installed"] = [s for s in reg.get("installed", []) if s.get("id") != skill_id]
        if len(reg["installed"]) == before:
            return {"ok": False, "error": f"skill not found: {skill_id}"}

        # Remove file
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", skill_id)
        for path in Path(self._install_dir).glob(f"{safe_id}*.md"):
            path.unlink()

        self._write_registry(reg)
        # 删除同步入库：skills/community 整目录重采集（capture 为整包替换）
        self._sync_install_dir()
        logger.info("uninstalled skill %s", skill_id)
        return {"ok": True, "skill_id": skill_id}

    def list_installed(self) -> List[Dict[str, Any]]:
        """Return list of installed skills from registry."""
        return self._read_registry().get("installed", [])

    async def browse_registry(self, query: str = "") -> List[Dict[str, Any]]:
        """Search available skills from the community hub."""
        if not self._client:
            return []
        url = f"{self._community_hub}registry.json"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            skills = data.get("skills", [])
            if query:
                q = query.lower()
                skills = [s for s in skills if q in s.get("title", "").lower() or q in s.get("description", "").lower()]
            return skills
        except Exception as exc:
            logger.warning("failed to browse registry: %s", exc)
            return []

    # --------------------------------------------------------- helpers
    def _resolve_source(self, source: str) -> Optional[str]:
        # GitHub owner/repo/path format
        m = re.match(r"^([a-zA-Z0-9_-]+)/([a-zA-Z0-9_.-]+)(?:@([a-zA-Z0-9_.-]+))?/(.+)$", source)
        if m:
            owner, repo, tag, path = m.groups()
            branch = tag or "main"
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        # Already a URL
        if source.startswith("https://raw.githubusercontent.com/"):
            return source
        return None

    def _parse_front_matter(self, content: str, source: str) -> Optional[SkillSpec]:
        import yaml
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
        if not m:
            return None
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except Exception:
            return None
        return SkillSpec(
            id=str(meta.get("id", source)),
            title=str(meta.get("title", meta.get("id", "unknown"))),
            description=str(meta.get("description", m.group(2)[:200])),
            version=str(meta.get("version", "1.0.0")),
            author=str(meta.get("author", "community")),
            tags=list(meta.get("tags", [])),
            raw_url="",
            checksum="",
        )