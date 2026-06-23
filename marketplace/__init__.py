"""Skill marketplace — publish, discover, and install skill packages.

A skill package is a directory containing:
  - SKILL.md (required): skill metadata and documentation
  - handler.py (optional): Python handler function
  - references/ (optional): reference documents
  - scripts/ (optional): helper scripts
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
from typing import Any, Dict, List, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)

# Official community hub base URL
COMMUNITY_HUB_URL = "https://raw.githubusercontent.com/huang2025511/one-agent-skills/main/"


class SkillPackage:
    """Represents a skill package."""
    
    def __init__(self, name: str, version: str = "1.0.0", description: str = "",
                 author: str = "", path: str = "") -> None:
        self.name = name
        self.version = version
        self.description = description
        self.author = author
        self.path = path
        self.sha256 = ""
        self.installed_at: Optional[float] = None
        self.tags: List[str] = []
        # 评分相关字段
        self.rating: float = 0.0
        self.rating_count: int = 0
    
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
            "rating": self.rating,
            "rating_count": self.rating_count,
        }


class Marketplace:
    """Skill marketplace for discovering and installing skill packages."""
    
    def __init__(self, registry_dir: str = "data/marketplace") -> None:
        self._registry_dir = Path(registry_dir)
        self._registry_dir.mkdir(parents=True, exist_ok=True)
        self._registry_file = self._registry_dir / "registry.json"
        self._packages: Dict[str, SkillPackage] = {}
        self._load_registry()
    
    def _load_registry(self) -> None:
        if self._registry_file.exists():
            try:
                data = json.loads(self._registry_file.read_text())
                for entry in data.get("packages", []):
                    pkg = SkillPackage(
                        name=entry["name"],
                        version=entry.get("version", "1.0.0"),
                        description=entry.get("description", ""),
                        author=entry.get("author", ""),
                    )
                    pkg.sha256 = entry.get("sha256", "")
                    pkg.tags = entry.get("tags", [])
                    # 恢复评分相关字段
                    pkg.rating = entry.get("rating", 0.0)
                    pkg.rating_count = entry.get("rating_count", 0)
                    self._packages[pkg.name] = pkg
            except Exception as exc:
                logger.warning("Failed to load marketplace registry: %s", exc)
    
    def _save_registry(self) -> None:
        # to_dict() 已包含 rating 和 rating_count，会一并持久化
        data = {
            "updated_at": time.time(),
            "packages": [p.to_dict() for p in self._packages.values()],
        }
        self._registry_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    
    def publish(self, dirpath: str) -> Optional[SkillPackage]:
        """Publish a skill package from a local directory."""
        pkg = SkillPackage.from_directory(dirpath)
        if pkg is None:
            return None
        pkg.compute_hash()
        
        # Copy to marketplace
        dest = self._registry_dir / pkg.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(dirpath, dest, dirs_exist_ok=True)
        
        self._packages[pkg.name] = pkg
        self._save_registry()
        logger.info("Published skill: %s v%s", pkg.name, pkg.version)
        return pkg
    
    def discover(self, query: str = "") -> List[Dict[str, Any]]:
        """Search available packages."""
        results = []
        for pkg in self._packages.values():
            if query and query.lower() not in pkg.name.lower() and query.lower() not in pkg.description.lower():
                continue
            results.append(pkg.to_dict())
        return sorted(results, key=lambda p: p["name"])
    
    def install(self, name: str, target_dir: str) -> bool:
        """Install a skill package to a target directory (e.g., ./skills/)."""
        if name not in self._packages:
            return False
        pkg = self._packages[name]
        src = self._registry_dir / pkg.name
        if not src.exists():
            return False
        dest = Path(target_dir) / pkg.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        logger.info("Installed skill: %s → %s", name, dest)
        return True
    
    def uninstall(self, name: str, target_dir: str) -> bool:
        """Remove an installed skill package."""
        dest = Path(target_dir) / name
        if not dest.exists():
            return False
        shutil.rmtree(dest)
        logger.info("Uninstalled skill: %s", name)
        return True
    
    def list_installed(self, target_dir: str) -> List[str]:
        """List installed skill packages."""
        path = Path(target_dir)
        if not path.exists():
            return []
        return [d.name for d in path.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]

    def rate_package(self, name: str, rating: float) -> bool:
        """为指定技能包打分。

        - rating 取值范围 1.0-5.0，超出范围返回 False
        - 包不存在返回 False
        - 使用加权平均更新评分，并持久化到 registry
        - 成功返回 True
        """
        # 校验评分范围
        if rating < 1.0 or rating > 5.0:
            return False
        # 校验包是否存在
        if name not in self._packages:
            return False
        pkg = self._packages[name]
        # 加权平均：新评分 = (旧评分 * 旧次数 + 本次评分) / (旧次数 + 1)
        pkg.rating = (pkg.rating * pkg.rating_count + rating) / (pkg.rating_count + 1)
        pkg.rating_count += 1
        # 持久化到 registry
        self._save_registry()
        logger.info("Rated skill %s: %.2f (count=%d)", name, pkg.rating, pkg.rating_count)
        return True

    def get_top_rated(self, limit: int = 10) -> List[Dict[str, Any]]:
        """按评分降序返回前 N 个技能包。"""
        packages = list(self._packages.values())
        # 按 rating 降序排序
        packages.sort(key=lambda p: p.rating, reverse=True)
        return [p.to_dict() for p in packages[:limit]]


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
        self._registry_path: Optional[str] = None
        self._install_dir: Optional[str] = None
        self._skills_plugin = None
        self._timeout = 30
        self._community_hub = COMMUNITY_HUB_URL

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("marketplace") or {}
        data_dir = ctx.config.get("agent", {}).get("data_dir", "./data")
        self._registry_path = os.path.join(data_dir, "marketplace", "registry.json")
        self._install_dir = os.path.join(data_dir, "skills", "community")
        self._community_hub = cfg.get("community_hub", COMMUNITY_HUB_URL)
        Path(self._registry_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self._install_dir).mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._ensure_registry()
        logger.info("marketplace ready, registry=%s", self._registry_path)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
        await super().stop()

    def _ensure_registry(self) -> None:
        if not Path(self._registry_path).exists():
            Path(self._registry_path).write_text(json.dumps({"installed": [], "available": []}, indent=2))

    def _read_registry(self) -> Dict[str, Any]:
        try:
            return json.loads(Path(self._registry_path).read_text(encoding="utf-8"))
        except Exception:
            return {"installed": [], "available": []}

    def _write_registry(self, data: Dict[str, Any]) -> None:
        Path(self._registry_path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    # --------------------------------------------------------- public API
    async def install(self, source: str) -> Dict[str, Any]:
        """Install a skill from:
          - GitHub: "owner/repo[@tag]/path"  (e.g. "octocat/Hello-World/readme.md")
          - URL: full raw URL to a .md file
        Returns {"ok": True, "skill": {...}} or {"ok": False, "error": "..."}
        """
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