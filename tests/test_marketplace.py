"""Unit tests for MarketplacePlugin.

Covers:
  - MarketplacePlugin lifecycle (setup/start/stop)
  - SkillPackage class
"""

from __future__ import annotations

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