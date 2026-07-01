"""Data Backup & Export — export and import agent data.

Provides data portability:
- Export all data to a single archive
- Import data from a backup archive
- Selective export (memory only, sessions only, etc.)
- Config export/import
- JSON/SQLite/Archive formats

Use cases:
- Migrate to a new server
- Backup before updates
- Share knowledge base with another agent
- Compliance data export
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


class ExportFormat(Enum):
    """Export format types."""
    ZIP = "zip"
    TAR_GZ = "tar.gz"
    JSON = "json"


class DataType(Enum):
    """Types of data that can be exported."""
    SESSIONS = "sessions"
    MEMORY = "memory"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    SKILLS = "skills"
    CONFIG = "config"
    AUDIT_LOG = "audit_log"
    ALL = "all"


@dataclass
class ExportResult:
    """Result of an export operation."""
    success: bool
    format: str
    file_path: str
    size_bytes: int
    items_exported: Dict[str, int]
    duration_seconds: float
    error: str = ""


@dataclass
class ImportResult:
    """Result of an import operation."""
    success: bool
    items_imported: Dict[str, int]
    duration_seconds: float
    error: str = ""


class DataExporter:
    """Export agent data to various formats."""

    def __init__(self, data_dir: str = "data") -> None:
        self._data_dir = Path(data_dir)
        self._memory_dir = self._data_dir / "memory"

    def export_all(
        self,
        output_path: str,
        format: ExportFormat = ExportFormat.ZIP,
        include_config: bool = True,
    ) -> ExportResult:
        """Export all data to an archive file."""
        start_time = time.time()
        items_exported: Dict[str, int] = {}

        try:
            if format == ExportFormat.ZIP:
                result = self._export_zip(output_path, include_config, items_exported)
            elif format == ExportFormat.TAR_GZ:
                result = self._export_tar_gz(output_path, include_config, items_exported)
            elif format == ExportFormat.JSON:
                result = self._export_json(output_path, include_config, items_exported)
            else:
                raise ValueError(f"Unsupported format: {format}")

            result.duration_seconds = time.time() - start_time
            return result

        except Exception as exc:
            logger.error("Export failed: %s", exc)
            return ExportResult(
                success=False,
                format=format.value,
                file_path=output_path,
                size_bytes=0,
                items_exported=items_exported,
                duration_seconds=time.time() - start_time,
                error=str(exc),
            )

    def export_data_type(
        self,
        data_type: DataType,
        output_path: str,
    ) -> ExportResult:
        """Export a specific type of data."""
        start_time = time.time()
        items_exported: Dict[str, int] = {}

        try:
            if data_type == DataType.SESSIONS:
                items_exported["sessions"] = self._export_sessions(output_path)
            elif data_type == DataType.MEMORY:
                items_exported["memory"] = self._export_memory(output_path)
            elif data_type == DataType.KNOWLEDGE_GRAPH:
                items_exported["knowledge_graph"] = self._export_kg(output_path)
            elif data_type == DataType.CONFIG:
                items_exported["config"] = self._export_config(output_path)
            else:
                raise ValueError(f"Cannot export single type: {data_type}")

            size = Path(output_path).stat().st_size if Path(output_path).exists() else 0

            return ExportResult(
                success=True,
                format="json",
                file_path=output_path,
                size_bytes=size,
                items_exported=items_exported,
                duration_seconds=time.time() - start_time,
            )

        except Exception as exc:
            return ExportResult(
                success=False,
                format="json",
                file_path=output_path,
                size_bytes=0,
                items_exported=items_exported,
                duration_seconds=time.time() - start_time,
                error=str(exc),
            )

    def _export_zip(
        self,
        output_path: str,
        include_config: bool,
        items_exported: Dict[str, int],
    ) -> ExportResult:
        """Export to ZIP format."""
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Export sessions
            sessions = self._export_sessions_to_json()
            if sessions:
                zf.writestr("sessions.json", json.dumps(sessions, ensure_ascii=False))
                items_exported["sessions"] = len(sessions.get("sessions", []))

            # Export memory
            memory = self._export_memory_to_json()
            if memory:
                zf.writestr("memory.json", json.dumps(memory, ensure_ascii=False))
                items_exported["memory_entries"] = memory.get("total_entries", 0)

            # Export knowledge graph
            kg = self._export_kg_to_json()
            if kg:
                zf.writestr("knowledge_graph.json", json.dumps(kg, ensure_ascii=False))
                items_exported["entities"] = kg.get("entity_count", 0)

            # Export config
            if include_config:
                config = self._export_config_to_json()
                if config:
                    zf.writestr("config.json", json.dumps(config, ensure_ascii=False))
                    items_exported["config"] = 1

            # Add manifest
            manifest = {
                "version": "1.0",
                "exported_at": time.time(),
                "items": list(items_exported.keys()),
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        size = Path(output_path).stat().st_size
        return ExportResult(
            success=True,
            format=ExportFormat.ZIP.value,
            file_path=output_path,
            size_bytes=size,
            items_exported=items_exported,
            duration_seconds=0,
        )

    def _export_tar_gz(
        self,
        output_path: str,
        include_config: bool,
        items_exported: Dict[str, int],
    ) -> ExportResult:
        """Export to tar.gz format."""
        import gzip

        with tarfile.open(output_path, "w:gz") as tf:
            # Add all files from data directory
            for db_file in self._memory_dir.glob("*.db"):
                tf.add(db_file, arcname=f"memory/{db_file.name}")

            if include_config:
                config_file = Path("config/default_config.yaml")
                if config_file.exists():
                    tf.add(config_file, arcname="config/default_config.yaml")

            # Add JSON exports
            sessions = self._export_sessions_to_json()
            if sessions:
                self._add_json_to_tar(tf, "sessions.json", sessions)

            items_exported["files"] = len(tf.getnames())

        size = Path(output_path).stat().st_size
        return ExportResult(
            success=True,
            format=ExportFormat.TAR_GZ.value,
            file_path=output_path,
            size_bytes=size,
            items_exported=items_exported,
            duration_seconds=0,
        )

    def _export_json(
        self,
        output_path: str,
        include_config: bool,
        items_exported: Dict[str, int],
    ) -> ExportResult:
        """Export to single JSON file."""
        data = {
            "version": "1.0",
            "exported_at": time.time(),
            "sessions": self._export_sessions_to_json(),
            "memory": self._export_memory_to_json(),
            "knowledge_graph": self._export_kg_to_json(),
        }

        if include_config:
            data["config"] = self._export_config_to_json()

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        size = Path(output_path).stat().st_size
        return ExportResult(
            success=True,
            format=ExportFormat.JSON.value,
            file_path=output_path,
            size_bytes=size,
            items_exported=items_exported,
            duration_seconds=0,
        )

    def _export_sessions(self, output_path: str) -> int:
        """Export sessions to JSON file."""
        data = self._export_sessions_to_json()
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data.get("session_count", 0)

    def _export_sessions_to_json(self) -> Dict[str, Any]:
        """Export sessions as JSON dict."""
        db_path = self._memory_dir / "sessions.db"
        if not db_path.exists():
            return {}

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            # Get sessions
            cur = conn.execute(
                "SELECT session_id, created_at, updated_at, message_count "
                "FROM sessions ORDER BY updated_at DESC LIMIT 1000"
            )
            sessions = [dict(row) for row in cur.fetchall()]

            conn.close()

            return {
                "session_count": len(sessions),
                "sessions": sessions,
            }
        except Exception as exc:
            logger.warning("Failed to export sessions: %s", exc)
            return {}

    def _export_memory(self, output_path: str) -> int:
        """Export memory to JSON file."""
        data = self._export_memory_to_json()
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data.get("total_entries", 0)

    def _export_memory_to_json(self) -> Dict[str, Any]:
        """Export memory as JSON dict."""
        db_path = self._memory_dir / "embeddings.db"
        if not db_path.exists():
            return {}

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            cur = conn.execute(
                "SELECT text, created_at FROM embeddings ORDER BY created_at DESC LIMIT 5000"
            )
            entries = [dict(row) for row in cur.fetchall()]

            conn.close()

            return {
                "total_entries": len(entries),
                "entries": entries,
            }
        except Exception as exc:
            logger.warning("Failed to export memory: %s", exc)
            return {}

    def _export_kg(self, output_path: str) -> int:
        """Export knowledge graph to JSON file."""
        data = self._export_kg_to_json()
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data.get("entity_count", 0)

    def _export_kg_to_json(self) -> Dict[str, Any]:
        """Export knowledge graph as JSON dict."""
        db_path = self._memory_dir / "kg.db"
        if not db_path.exists():
            return {}

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            # Get entities
            cur = conn.execute("SELECT name, entity_type, created_at FROM entities LIMIT 5000")
            entities = [dict(row) for row in cur.fetchall()]

            # Get relations
            cur = conn.execute(
                "SELECT subject_name, predicate, object_name FROM relations LIMIT 10000"
            )
            relations = [dict(row) for row in cur.fetchall()]

            conn.close()

            return {
                "entity_count": len(entities),
                "relation_count": len(relations),
                "entities": entities,
                "relations": relations,
            }
        except Exception as exc:
            logger.warning("Failed to export knowledge graph: %s", exc)
            return {}

    def _export_config(self, output_path: str) -> int:
        """Export config to JSON file."""
        data = self._export_config_to_json()
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return 1

    def _export_config_to_json(self) -> Dict[str, Any]:
        """Export config as JSON dict."""
        import yaml

        config_path = Path("config/default_config.yaml")
        if not config_path.exists():
            return {}

        try:
            with open(config_path, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            return config or {}
        except Exception as exc:
            logger.warning("Failed to export config: %s", exc)
            return {}

    def _add_json_to_tar(self, tf: tarfile.TarFile, name: str, data: Dict) -> None:
        """Add JSON data to tar file."""
        import io
        json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        info = tarfile.TarInfo(name=name)
        info.size = len(json_bytes)
        tf.addfile(info, io.BytesIO(json_bytes))


class DataImporter:
    """Import agent data from backup archives."""

    def __init__(self, data_dir: str = "data") -> None:
        self._data_dir = Path(data_dir)
        self._memory_dir = self._data_dir / "memory"

    def import_from_file(
        self,
        file_path: str,
        merge: bool = True,
    ) -> ImportResult:
        """Import data from a backup file."""
        start_time = time.time()
        items_imported: Dict[str, int] = {}

        try:
            path = Path(file_path)
            if path.suffix == ".zip":
                items_imported = self._import_zip(file_path, merge)
            elif path.suffix == ".json":
                items_imported = self._import_json(file_path, merge)
            else:
                raise ValueError(f"Unsupported file format: {path.suffix}")

            return ImportResult(
                success=True,
                items_imported=items_imported,
                duration_seconds=time.time() - start_time,
            )

        except Exception as exc:
            logger.error("Import failed: %s", exc)
            return ImportResult(
                success=False,
                items_imported=items_imported,
                duration_seconds=time.time() - start_time,
                error=str(exc),
            )

    def _import_zip(self, file_path: str, merge: bool) -> Dict[str, int]:
        """Import from ZIP archive."""
        items_imported: Dict[str, int] = {}

        with zipfile.ZipFile(file_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    content = zf.read(name).decode("utf-8")
                    data = json.loads(content)

                    if "sessions" in name:
                        items_imported["sessions"] = self._import_sessions(data, merge)
                    elif "memory" in name:
                        items_imported["memory"] = self._import_memory(data, merge)
                    elif "knowledge_graph" in name:
                        items_imported["knowledge_graph"] = self._import_kg(data, merge)
                    elif "config" in name:
                        items_imported["config"] = self._import_config(data, merge)

        return items_imported

    def _import_json(self, file_path: str, merge: bool) -> Dict[str, int]:
        """Import from JSON file."""
        items_imported: Dict[str, int] = {}

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        if "sessions" in data:
            items_imported["sessions"] = self._import_sessions(data["sessions"], merge)
        if "memory" in data:
            items_imported["memory"] = self._import_memory(data["memory"], merge)
        if "knowledge_graph" in data:
            items_imported["knowledge_graph"] = self._import_kg(data["knowledge_graph"], merge)
        if "config" in data:
            items_imported["config"] = self._import_config(data["config"], merge)

        return items_imported

    def _import_sessions(self, data: Dict, merge: bool) -> int:
        """Import sessions into database."""
        if not data.get("sessions"):
            return 0

        db_path = self._memory_dir / "sessions.db"
        if not db_path.exists():
            return 0

        count = 0
        try:
            conn = sqlite3.connect(str(db_path))
            for session in data["sessions"]:
                if merge:
                    conn.execute(
                        "INSERT OR IGNORE INTO sessions(session_id, created_at, updated_at, message_count) "
                        "VALUES (?, ?, ?, ?)",
                        (session["session_id"], session["created_at"], session["updated_at"],
                         session.get("message_count", 0)),
                    )
                else:
                    conn.execute(
                        "INSERT INTO sessions(session_id, created_at, updated_at, message_count) "
                        "VALUES (?, ?, ?, ?)",
                        (session["session_id"], session["created_at"], session["updated_at"],
                         session.get("message_count", 0)),
                    )
                count += 1
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("Failed to import sessions: %s", exc)

        return count

    def _import_memory(self, data: Dict, merge: bool) -> int:
        """Import memory into database."""
        if not data.get("entries"):
            return 0

        db_path = self._memory_dir / "embeddings.db"
        if not db_path.exists():
            return 0

        count = 0
        try:
            conn = sqlite3.connect(str(db_path))
            for entry in data["entries"]:
                conn.execute(
                    "INSERT OR IGNORE INTO embeddings(text, created_at) VALUES (?, ?)",
                    (entry["text"], entry["created_at"]),
                )
                count += 1
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("Failed to import memory: %s", exc)

        return count

    def _import_kg(self, data: Dict, merge: bool) -> int:
        """Import knowledge graph into database."""
        if not data.get("entities"):
            return 0

        db_path = self._memory_dir / "kg.db"
        if not db_path.exists():
            return 0

        count = 0
        try:
            conn = sqlite3.connect(str(db_path))
            for entity in data["entities"]:
                conn.execute(
                    "INSERT OR IGNORE INTO entities(name, entity_type, created_at) VALUES (?, ?, ?)",
                    (entity["name"], entity.get("entity_type", ""), entity["created_at"]),
                )
                count += 1
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("Failed to import knowledge graph: %s", exc)

        return count

    def _import_config(self, data: Dict, merge: bool) -> int:
        """Import config into YAML file."""
        import yaml

        config_path = Path("config/default_config.yaml")
        try:
            if merge:
                # Load existing and merge
                with open(config_path, encoding="utf-8") as f:
                    existing = yaml.safe_load(f) or {}
                existing.update(data)
                data = existing

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
            return 1
        except Exception as exc:
            logger.warning("Failed to import config: %s", exc)
            return 0