"""Configuration backup and restore system.

Provides:
  - Automatic backup before config changes
  - Version history with timestamps
  - Atomic restore operations
  - Backup rotation (keep last N versions)

v2.1.0 数据统一：备份内容不再写 config/backups/*.yaml，改存统一库
{data_dir}/one_agent.db 的 config_backups 表（core.hub.Hub.backup_*）。
旧目录下存量 *.yaml 首次初始化时一次性导入并改名 backups.migrated 兜底。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from core.hub import get_hub

logger = logging.getLogger(__name__)


class ConfigBackupManager:
    """Manages configuration backups with versioning and rotation.

    Backups are stored in the unified database (config_backups table)
    via :class:`core.hub.Hub`. Public method signatures stay compatible
    with the historical file-based implementation.
    """

    def __init__(self, config_path: str, backup_dir: Optional[str] = None) -> None:
        self._config_path = Path(config_path)
        # 旧版文件备份目录 — 仅用于一次性迁移检测，新备份不再写盘
        self._backup_dir = Path(backup_dir or self._config_path.parent / "backups")
        self._max_backups = 10  # Keep last 10 versions
        self._hub = get_hub()
        self._migrate_legacy_backups()

    # ------------------------------------------------------------- legacy 迁移

    def _migrate_legacy_backups(self) -> None:
        """一次性迁移：config/backups/*.yaml → 统一库 config_backups 表。

        仅当目录存在 *.yaml 且表中无任何备份时执行；全部导入成功后把
        目录改名为 backups.migrated 保留兜底。
        """
        if not self._backup_dir.is_dir():
            return
        yaml_files = sorted(self._backup_dir.glob("*.yaml"))
        if not yaml_files:
            return
        if self._hub.backup_list():
            return  # 表中已有备份，避免重复导入

        reasons = self._load_legacy_reasons()
        for p in yaml_files:
            reason = reasons.get(p.name, self._reason_from_filename(p.name))
            try:
                self._hub.backup_put(
                    p.name, p.read_text(encoding="utf-8"), reason=reason)
            except Exception as exc:
                logger.warning(
                    "legacy config backup import failed for %s (left in place): %s",
                    p.name, exc)
                return

        try:
            self._backup_dir.rename(
                self._backup_dir.with_name(self._backup_dir.name + ".migrated"))
            logger.info(
                "migrated %d legacy config backups into unified db, "
                "dir renamed to %s.migrated", len(yaml_files), self._backup_dir.name)
        except OSError as exc:
            logger.warning("legacy backup dir rename failed: %s", exc)

    def _load_legacy_reasons(self) -> Dict[str, str]:
        """读取旧版 backup_index.json 的 filename → reason 映射（尽力而为）。"""
        index_file = self._backup_dir / "backup_index.json"
        if not index_file.exists():
            return {}
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                entries = json.load(f)
            return {e["filename"]: e.get("reason", "") for e in entries
                    if isinstance(e, dict) and e.get("filename")}
        except Exception as exc:
            logger.warning("failed to load legacy backup index: %s", exc)
            return {}

    @staticmethod
    def _reason_from_filename(name: str) -> str:
        """从 config_YYYYmmdd_HHMMSS_{reason}.yaml 文件名解析 reason。"""
        stem = name[:-5] if name.endswith(".yaml") else name
        parts = stem.split("_")
        return "_".join(parts[3:]) if len(parts) > 3 else "migrated"

    # ------------------------------------------------------------- 备份 CRUD

    def create_backup(self, reason: str = "manual") -> Optional[str]:
        """Create a backup of the current config.

        Args:
            reason: Reason for backup (e.g., "manual", "pre-change", "scheduled")

        Returns:
            Backup filename if successful, None otherwise
        """
        if not self._config_path.exists():
            logger.warning("config file does not exist: %s", self._config_path)
            return None

        try:
            content = self._config_path.read_text(encoding="utf-8")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"config_{timestamp}_{reason}.yaml"

            self._hub.backup_put(backup_name, content, reason=reason)
            self._rotate_backups()

            logger.info("config backup created: %s (reason=%s)", backup_name, reason)
            return backup_name

        except Exception as exc:
            logger.exception("failed to create config backup: %s", exc)
            return None

    def restore_backup(self, backup_name: Optional[str] = None) -> bool:
        """Restore config from a backup.

        Args:
            backup_name: Specific backup to restore. If None, restores most recent.

        Returns:
            True if successful, False otherwise
        """
        backups = self.list_backups()
        if not backups:
            logger.warning("no backups available")
            return False

        if backup_name is None:
            # Use most recent
            backup_entry = backups[-1]
        else:
            backup_entry = next(
                (b for b in backups if b["filename"] == backup_name), None)
            if backup_entry is None:
                logger.error("backup not found: %s", backup_name)
                return False

        row = self._hub.backup_get(backup_entry["filename"])
        if row is None:
            logger.error("backup missing in unified db: %s", backup_entry["filename"])
            return False

        try:
            # Create a pre-restore backup
            self.create_backup(reason="pre-restore")

            # Atomic restore: write to temp file, then rename
            temp_path = self._config_path.with_suffix(".yaml.tmp")
            temp_path.write_text(row["content"], encoding="utf-8")
            os.replace(temp_path, self._config_path)

            logger.info("config restored from backup: %s", backup_entry["filename"])
            return True

        except Exception as exc:
            logger.exception("failed to restore config: %s", exc)
            # Clean up temp file if it exists
            temp_path = self._config_path.with_suffix(".yaml.tmp")
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception as unlink_exc:
                    logger.debug("ignored non-critical error: %s", unlink_exc)
            return False

    def list_backups(self) -> List[Dict[str, Any]]:
        """List all available backups (oldest first, latest last)."""
        rows = self._hub.backup_list()  # created_at DESC
        return [
            {
                "filename": r["name"],
                "timestamp": r["created_at"],
                "datetime": datetime.fromtimestamp(r["created_at"]).isoformat(),
                "reason": r["reason"] or "",
                "size_bytes": r["size_bytes"] or 0,
            }
            for r in reversed(rows)
        ]

    def delete_backup(self, backup_name: str) -> bool:
        """Delete a specific backup."""
        if self._hub.backup_get(backup_name) is None:
            logger.warning("backup not found: %s", backup_name)
            return False
        try:
            self._hub.backup_delete(backup_name)
            logger.info("backup deleted: %s", backup_name)
            return True
        except Exception as exc:
            logger.exception("failed to delete backup: %s", exc)
            return False

    def _rotate_backups(self) -> None:
        """Remove old backups to maintain max_backups limit."""
        rows = self._hub.backup_list()  # created_at DESC — newest first
        for row in rows[self._max_backups:]:
            try:
                self._hub.backup_delete(row["name"])
                logger.debug("rotated old backup: %s", row["name"])
            except Exception as exc:
                logger.warning("failed to rotate backup %s: %s", row["name"], exc)

    def get_backup_content(self, backup_name: str) -> Optional[Dict[str, Any]]:
        """Read and parse a backup from the unified database."""
        row = self._hub.backup_get(backup_name)
        if row is None:
            return None

        try:
            return yaml.safe_load(row["content"]) or {}
        except Exception as exc:
            logger.exception("failed to read backup %s: %s", backup_name, exc)
            return None

    def diff_with_current(self, backup_name: str) -> Optional[Dict[str, Any]]:
        """Compare a backup with current config."""
        backup_content = self.get_backup_content(backup_name)
        if backup_content is None:
            return None

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                current_content = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.exception("failed to read current config: %s", exc)
            return None

        # Simple diff: show keys that differ
        diff = {
            "added": [],
            "removed": [],
            "modified": [],
        }

        all_keys = set(backup_content.keys()) | set(current_content.keys())
        for key in all_keys:
            if key not in backup_content:
                diff["added"].append(key)
            elif key not in current_content:
                diff["removed"].append(key)
            elif backup_content[key] != current_content[key]:
                diff["modified"].append(key)

        return diff
