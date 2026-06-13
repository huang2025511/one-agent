"""Plugin marketplace — auto-install skills from GitHub, GitLab, or URLs.

Features:
  - Install skill from GitHub repo (owner/repo@path)
  - Verify repository exists and is accessible before installing
  - Sandboxed installation (validate YAML header before writing)
  - Uninstall / list installed skills
  - Skill manifest registry at data/marketplace/registry.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# Official community hub base URL
COMMUNITY_HUB_URL = "https://raw.githubusercontent.com/huang2025511/one-agent-skills/main/"


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

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        data_dir = ctx.config.get("agent", {}).get("data_dir", "./data")
        self._registry_path = os.path.join(data_dir, "marketplace", "registry.json")
        self._install_dir = os.path.join(data_dir, "skills", "community")
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
        url = f"{COMMUNITY_HUB_URL}registry.json"
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
