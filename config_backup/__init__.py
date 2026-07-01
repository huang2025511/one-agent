"""Configuration backup and restore system.

Provides:
  - Automatic backup before config changes
  - Version history with timestamps
  - Atomic restore operations
  - Backup rotation (keep last N versions)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


class ConfigBackupManager:
    """Manages configuration backups with versioning and rotation."""

    def __init__(self, config_path: str, backup_dir: Optional[str] = None) -> None:
        self._config_path = Path(config_path)
        self._backup_dir = Path(backup_dir or self._config_path.parent / "backups")
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._max_backups = 10  # Keep last 10 versions
        self._index_file = self._backup_dir / "backup_index.json"
        self._index = self._load_index()

    def _load_index(self) -> List[Dict[str, Any]]:
        """Load backup index metadata."""
        if self._index_file.exists():
            try:
                with open(self._index_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("failed to load backup index: %s", exc)
        return []

    def _save_index(self) -> None:
        """Save backup index metadata."""
        try:
            with open(self._index_file, "w", encoding="utf-8") as f:
                json.dump(self._index, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("failed to save backup index: %s", exc)

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
            # Generate timestamped filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"config_{timestamp}_{reason}.yaml"
            backup_path = self._backup_dir / backup_name

            # Copy config to backup location
            shutil.copy2(self._config_path, backup_path)

            # Update index
            self._index.append({
                "filename": backup_name,
                "timestamp": time.time(),
                "datetime": datetime.now().isoformat(),
                "reason": reason,
                "size_bytes": backup_path.stat().st_size,
            })

            # Rotate old backups
            self._rotate_backups()

            # Save index
            self._save_index()

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
        if not self._index:
            logger.warning("no backups available")
            return False

        # Find backup to restore
        if backup_name is None:
            # Use most recent
            backup_entry = self._index[-1]
        else:
            backup_entry = next((b for b in self._index if b["filename"] == backup_name), None)
            if backup_entry is None:
                logger.error("backup not found: %s", backup_name)
                return False

        backup_path = self._backup_dir / backup_entry["filename"]
        if not backup_path.exists():
            logger.error("backup file missing: %s", backup_path)
            return False

        try:
            # Create a pre-restore backup
            self.create_backup(reason="pre-restore")

            # Atomic restore: write to temp file, then rename
            temp_path = self._config_path.with_suffix(".yaml.tmp")
            shutil.copy2(backup_path, temp_path)
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
                except Exception:
                    pass
            return False

    def list_backups(self) -> List[Dict[str, Any]]:
        """List all available backups."""
        return [
            {
                "filename": b["filename"],
                "timestamp": b["timestamp"],
                "datetime": b["datetime"],
                "reason": b["reason"],
                "size_bytes": b["size_bytes"],
            }
            for b in self._index
        ]

    def delete_backup(self, backup_name: str) -> bool:
        """Delete a specific backup."""
        backup_entry = next((b for b in self._index if b["filename"] == backup_name), None)
        if backup_entry is None:
            logger.warning("backup not found: %s", backup_name)
            return False

        backup_path = self._backup_dir / backup_name
        try:
            if backup_path.exists():
                backup_path.unlink()
            self._index = [b for b in self._index if b["filename"] != backup_name]
            self._save_index()
            logger.info("backup deleted: %s", backup_name)
            return True
        except Exception as exc:
            logger.exception("failed to delete backup: %s", exc)
            return False

    def _rotate_backups(self) -> None:
        """Remove old backups to maintain max_backups limit."""
        if len(self._index) <= self._max_backups:
            return

        # Sort by timestamp (oldest first)
        sorted_backups = sorted(self._index, key=lambda b: b["timestamp"])
        to_remove = sorted_backups[:len(sorted_backups) - self._max_backups]

        for backup in to_remove:
            backup_path = self._backup_dir / backup["filename"]
            try:
                if backup_path.exists():
                    backup_path.unlink()
                logger.debug("rotated old backup: %s", backup["filename"])
            except Exception as exc:
                logger.warning("failed to rotate backup %s: %s", backup["filename"], exc)

        # Update index
        self._index = [b for b in self._index if b not in to_remove]

    def get_backup_content(self, backup_name: str) -> Optional[Dict[str, Any]]:
        """Read and parse a backup file."""
        backup_path = self._backup_dir / backup_name
        if not backup_path.exists():
            return None

        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
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
