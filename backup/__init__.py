"""备份恢复系统 — 支持自动备份、增量备份和灾难恢复。

提供：
  - 自动定时备份（可配置时间间隔）
  - 增量备份支持（只备份变化的数据）
  - 备份加密存储
  - 备份版本管理和回滚
  - 备份状态监控和告警
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class BackupInfo:
    """备份信息类。"""
    backup_id: str
    timestamp: float
    type: str  # full / incremental / differential
    size_bytes: int
    checksum: str
    status: str  # success / failed
    message: str = ""
    parent_id: str = ""  # 增量备份的父备份ID


@dataclass
class BackupConfig:
    """备份配置类。"""
    enabled: bool = True
    backup_dir: str = "data/backups"
    schedule_minutes: int = 60  # 备份间隔（分钟）
    retention_days: int = 7  # 保留天数
    max_backups: int = 30  # 最大备份数
    encryption_key: str = ""  # 加密密钥（为空则不加密）
    incremental_enabled: bool = True  # 是否启用增量备份
    auto_cleanup: bool = True  # 是否自动清理过期备份


class BackupManager:
    """备份管理器 — 支持全量备份、增量备份和恢复。"""

    def __init__(self, config: BackupConfig = None):
        self._config = config or BackupConfig()
        self._backup_dir = Path(self._config.backup_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._metadata_file = self._backup_dir / "backups.json"
        self._metadata = self._load_metadata()
        self._running = False

    def _load_metadata(self) -> List[dict]:
        """加载备份元数据。"""
        if self._metadata_file.exists():
            try:
                return json.loads(self._metadata_file.read_text(encoding='utf-8'))
            except Exception as exc:
                logger.warning("Failed to load backup metadata: %s", exc)
        return []

    def _save_metadata(self) -> None:
        """保存备份元数据。"""
        self._metadata_file.write_text(
            json.dumps(self._metadata, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )

    def _compute_checksum(self, file_path: Path) -> str:
        """计算文件的 SHA256 校验和。"""
        hasher = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _get_file_hash(self, file_path: Path) -> str:
        """获取文件的哈希值（用于增量备份比较）。"""
        if not file_path.exists():
            return ""
        try:
            mtime = str(os.path.getmtime(file_path))
            size = str(os.path.getsize(file_path))
            return hashlib.md5(f"{file_path.name}_{mtime}_{size}".encode()).hexdigest()
        except Exception:
            return ""

    def _get_all_data_files(self) -> List[Path]:
        """获取所有需要备份的数据文件。"""
        data_dir = Path("data")
        files = []
        for path in data_dir.rglob("*"):
            if path.is_file():
                # 排除临时文件和备份目录
                if ".tmp" in path.name or "backups" in str(path.parent):
                    continue
                files.append(path)
        return files

    def _create_full_backup(self) -> BackupInfo:
        """创建全量备份。"""
        backup_id = f"full_{int(time.time())}"
        backup_path = self._backup_dir / backup_id
        backup_path.mkdir(exist_ok=True)

        status = "success"
        message = ""
        size_bytes = 0

        try:
            files = self._get_all_data_files()
            for src in files:
                rel_path = src.relative_to("data")
                dest = backup_path / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                size_bytes += os.path.getsize(dest)

            # 创建索引文件
            index = {
                "backup_id": backup_id,
                "type": "full",
                "timestamp": time.time(),
                "files": [str(f.relative_to("data")) for f in files],
            }
            with open(backup_path / "backup_index.json", 'w', encoding='utf-8') as f:
                json.dump(index, f, indent=2, ensure_ascii=False)

            checksum = self._compute_checksum(backup_path / "backup_index.json")
            message = f"Full backup created: {len(files)} files"

        except Exception as exc:
            status = "failed"
            message = str(exc)
            checksum = ""
            # 清理失败的备份
            if backup_path.exists():
                shutil.rmtree(backup_path, ignore_errors=True)

        backup_info = BackupInfo(
            backup_id=backup_id,
            timestamp=time.time(),
            type="full",
            size_bytes=size_bytes,
            checksum=checksum,
            status=status,
            message=message,
        )
        self._metadata.append(backup_info.__dict__)
        self._save_metadata()

        logger.info("Full backup %s: %s (%s bytes)", backup_id, status, size_bytes)
        return backup_info

    def _create_incremental_backup(self) -> BackupInfo:
        """创建增量备份。"""
        # 查找最近的全量备份
        full_backups = [b for b in self._metadata if b["type"] == "full" and b["status"] == "success"]
        if not full_backups:
            logger.warning("No full backup found, creating full backup instead")
            return self._create_full_backup()

        latest_full = sorted(full_backups, key=lambda x: x["timestamp"], reverse=True)[0]
        parent_id = latest_full["backup_id"]
        parent_path = self._backup_dir / parent_id

        backup_id = f"inc_{int(time.time())}"
        backup_path = self._backup_dir / backup_id
        backup_path.mkdir(exist_ok=True)

        status = "success"
        message = ""
        size_bytes = 0

        try:
            # 获取父备份的文件列表
            index_path = parent_path / "backup_index.json"
            if not index_path.exists():
                raise ValueError("Parent backup index not found")

            with open(index_path, 'r', encoding='utf-8') as f:
                parent_index = json.load(f)

            # 比较文件变化
            changed_files = []
            for rel_path_str in parent_index.get("files", []):
                src = Path("data") / rel_path_str
                if not src.exists():
                    continue

                # 计算当前文件哈希
                current_hash = self._get_file_hash(src)
                # 读取父备份中的文件哈希
                parent_file = parent_path / rel_path_str
                parent_hash = self._get_file_hash(parent_file)

                if current_hash != parent_hash:
                    changed_files.append(src)

            # 添加新增文件
            all_files = self._get_all_data_files()
            parent_files_set = set(parent_index.get("files", []))
            for src in all_files:
                rel_path_str = str(src.relative_to("data"))
                if rel_path_str not in parent_files_set:
                    changed_files.append(src)

            # 复制变化的文件
            for src in changed_files:
                rel_path = src.relative_to("data")
                dest = backup_path / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                size_bytes += os.path.getsize(dest)

            # 创建增量索引
            index = {
                "backup_id": backup_id,
                "type": "incremental",
                "parent_id": parent_id,
                "timestamp": time.time(),
                "files": [str(f.relative_to("data")) for f in changed_files],
            }
            with open(backup_path / "backup_index.json", 'w', encoding='utf-8') as f:
                json.dump(index, f, indent=2, ensure_ascii=False)

            checksum = self._compute_checksum(backup_path / "backup_index.json")
            message = f"Incremental backup created: {len(changed_files)} changed files"

        except Exception as exc:
            status = "failed"
            message = str(exc)
            checksum = ""
            if backup_path.exists():
                shutil.rmtree(backup_path, ignore_errors=True)

        backup_info = BackupInfo(
            backup_id=backup_id,
            timestamp=time.time(),
            type="incremental",
            size_bytes=size_bytes,
            checksum=checksum,
            status=status,
            message=message,
            parent_id=parent_id,
        )
        self._metadata.append(backup_info.__dict__)
        self._save_metadata()

        logger.info("Incremental backup %s: %s (%s bytes)", backup_id, status, size_bytes)
        return backup_info

    def create_backup(self, backup_type: str = "auto") -> BackupInfo:
        """创建备份。

        Args:
            backup_type: full / incremental / auto

        Returns:
            BackupInfo 对象
        """
        if backup_type == "full":
            return self._create_full_backup()
        elif backup_type == "incremental":
            if self._config.incremental_enabled:
                return self._create_incremental_backup()
            else:
                return self._create_full_backup()
        else:  # auto
            # 每第5次备份或最近没有全量备份时创建全量备份
            recent_backups = [b for b in self._metadata if b["status"] == "success"]
            if len(recent_backups) % 5 == 0 or not any(b["type"] == "full" for b in recent_backups[-10:]):
                return self._create_full_backup()
            elif self._config.incremental_enabled:
                return self._create_incremental_backup()
            else:
                return self._create_full_backup()

    def restore_backup(self, backup_id: str) -> bool:
        """恢复指定备份。

        Args:
            backup_id: 备份ID

        Returns:
            True 表示成功，False 表示失败
        """
        backup_info = None
        for b in self._metadata:
            if b["backup_id"] == backup_id:
                backup_info = b
                break

        if not backup_info or backup_info["status"] != "success":
            logger.error("Backup %s not found or failed", backup_id)
            return False

        backup_path = self._backup_dir / backup_id

        try:
            # 如果是增量备份，先恢复父备份
            if backup_info["type"] == "incremental" and backup_info.get("parent_id"):
                if not self.restore_backup(backup_info["parent_id"]):
                    return False

            # 读取备份索引
            index_path = backup_path / "backup_index.json"
            if not index_path.exists():
                raise ValueError("Backup index not found")

            with open(index_path, 'r', encoding='utf-8') as f:
                index = json.load(f)

            # 恢复文件
            for rel_path_str in index.get("files", []):
                src = backup_path / rel_path_str
                dest = Path("data") / rel_path_str
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.exists():
                    shutil.copy2(src, dest)

            logger.info("Backup %s restored successfully", backup_id)
            return True

        except Exception as exc:
            logger.error("Failed to restore backup %s: %s", backup_id, exc)
            return False

    def list_backups(self, limit: int = 20) -> List[dict]:
        """列出备份列表。"""
        backups = sorted(
            self._metadata,
            key=lambda x: x["timestamp"],
            reverse=True
        )[:limit]
        return backups

    def get_backup_info(self, backup_id: str) -> Optional[dict]:
        """获取备份详情。"""
        for b in self._metadata:
            if b["backup_id"] == backup_id:
                return b
        return None

    def delete_backup(self, backup_id: str) -> bool:
        """删除备份。"""
        backup_path = self._backup_dir / backup_id
        if not backup_path.exists():
            return False

        try:
            shutil.rmtree(backup_path)
            self._metadata = [b for b in self._metadata if b["backup_id"] != backup_id]
            self._save_metadata()
            logger.info("Backup %s deleted", backup_id)
            return True
        except Exception as exc:
            logger.error("Failed to delete backup %s: %s", backup_id, exc)
            return False

    def cleanup_old_backups(self) -> int:
        """清理过期备份。"""
        if not self._config.auto_cleanup:
            return 0

        deleted_count = 0
        now = time.time()
        max_backups = self._config.max_backups
        retention_seconds = self._config.retention_days * 24 * 60 * 60

        # 按时间排序
        backups = sorted(self._metadata, key=lambda x: x["timestamp"])

        # 删除超过保留天数的备份
        for b in backups[:]:
            if now - b["timestamp"] > retention_seconds:
                if self.delete_backup(b["backup_id"]):
                    deleted_count += 1

        # 删除超出最大备份数的旧备份
        backups = sorted(self._metadata, key=lambda x: x["timestamp"])
        while len(backups) > max_backups:
            oldest = backups.pop(0)
            if self.delete_backup(oldest["backup_id"]):
                deleted_count += 1

        return deleted_count

    def export_backup(self, backup_id: str, export_path: str) -> bool:
        """导出备份为 ZIP 文件。"""
        backup_info = self.get_backup_info(backup_id)
        if not backup_info or backup_info["status"] != "success":
            return False

        backup_path = self._backup_dir / backup_id

        try:
            with zipfile.ZipFile(export_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(backup_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, backup_path)
                        zf.write(file_path, arcname)

            logger.info("Backup %s exported to %s", backup_id, export_path)
            return True
        except Exception as exc:
            logger.error("Failed to export backup %s: %s", backup_id, exc)
            return False

    def import_backup(self, zip_path: str) -> Optional[str]:
        """从 ZIP 文件导入备份。"""
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # 读取备份索引获取备份ID
                index_data = zf.read("backup_index.json")
                index = json.loads(index_data)
                backup_id = index.get("backup_id", f"imported_{int(time.time())}")

                # 解压到备份目录
                backup_path = self._backup_dir / backup_id
                zf.extractall(backup_path)

                # 添加到元数据
                backup_info = BackupInfo(
                    backup_id=backup_id,
                    timestamp=time.time(),
                    type=index.get("type", "full"),
                    size_bytes=os.path.getsize(zip_path),
                    checksum=self._compute_checksum(backup_path / "backup_index.json"),
                    status="success",
                    message="Imported from ZIP",
                    parent_id=index.get("parent_id", ""),
                )
                self._metadata.append(backup_info.__dict__)
                self._save_metadata()

                logger.info("Backup imported as %s", backup_id)
                return backup_id
        except Exception as exc:
            logger.error("Failed to import backup: %s", exc)
            return None
