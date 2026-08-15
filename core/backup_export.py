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
import tarfile
import time
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List

from core.db import create_sqlite_connection

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
        # v2.1.0 数据统一：所有业务表都在统一库
        self._db_path = self._data_dir / "one_agent.db"

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
            # v2.1.0：附上统一库原文件（checkpoint 后拷入），此 zip 本身
            # 即可直接用于部署新环境
            if self._db_path.exists():
                try:
                    from core.hub import get_hub
                    get_hub(str(self._data_dir)).checkpoint()
                except Exception as exc:
                    logger.debug("pre-export checkpoint failed: %s", exc)
                zf.write(self._db_path, arcname="one_agent.db")
                items_exported["database"] = 1

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

        with tarfile.open(output_path, "w:gz") as tf:
            # v2.1.0：附上统一库原文件（checkpoint 后拷入）
            if self._db_path.exists():
                try:
                    from core.hub import get_hub
                    get_hub(str(self._data_dir)).checkpoint()
                except Exception as exc:
                    logger.debug("pre-export checkpoint failed: %s", exc)
                tf.add(self._db_path, arcname="one_agent.db")
                items_exported["database"] = 1

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
        """Export sessions as JSON dict（统一库，列名与实际 schema 对齐）."""
        if not self._db_path.exists():
            return {}

        conn = None
        try:
            conn = create_sqlite_connection(str(self._db_path))
            cur = conn.execute(
                "SELECT id AS session_id, title, created_at, updated_at, "
                "message_count, total_tokens, status "
                "FROM sessions ORDER BY updated_at DESC LIMIT 1000"
            )
            sessions = [dict(row) for row in cur.fetchall()]
            return {
                "session_count": len(sessions),
                "sessions": sessions,
            }
        except Exception as exc:
            logger.warning("Failed to export sessions: %s", exc)
            return {}
        finally:
            if conn:
                conn.close()

    def _export_memory(self, output_path: str) -> int:
        """Export memory to JSON file."""
        data = self._export_memory_to_json()
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data.get("total_entries", 0)

    def _export_memory_to_json(self) -> Dict[str, Any]:
        """Export memory as JSON dict（统一库 FTS memory 表）."""
        if not self._db_path.exists():
            return {}

        conn = None
        try:
            conn = create_sqlite_connection(str(self._db_path))
            # memory 是 FTS5 虚拟表（content/source/tags/timestamp）；
            # 按 rowid 倒序取最近条目
            cur = conn.execute(
                "SELECT content AS text, source, tags, timestamp AS created_at "
                "FROM memory ORDER BY rowid DESC LIMIT 5000"
            )
            entries = [dict(row) for row in cur.fetchall()]
            return {
                "total_entries": len(entries),
                "entries": entries,
            }
        except Exception as exc:
            logger.warning("Failed to export memory: %s", exc)
            return {}
        finally:
            if conn:
                conn.close()

    def _export_kg(self, output_path: str) -> int:
        """Export knowledge graph to JSON file."""
        data = self._export_kg_to_json()
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data.get("entity_count", 0)

    def _export_kg_to_json(self) -> Dict[str, Any]:
        """Export knowledge graph as JSON dict（统一库，JOIN 解析关系端点名）."""
        if not self._db_path.exists():
            return {}

        conn = None
        try:
            conn = create_sqlite_connection(str(self._db_path))
            cur = conn.execute(
                "SELECT name, type AS entity_type, created_at FROM entities LIMIT 5000")
            entities = [dict(row) for row in cur.fetchall()]

            cur = conn.execute(
                "SELECT s.name AS subject_name, r.predicate, o.name AS object_name "
                "FROM relations r "
                "JOIN entities s ON s.id = r.subject_id "
                "JOIN entities o ON o.id = r.object_id LIMIT 10000")
            relations = [dict(row) for row in cur.fetchall()]

            return {
                "entity_count": len(entities),
                "relation_count": len(relations),
                "entities": entities,
                "relations": relations,
            }
        except Exception as exc:
            logger.warning("Failed to export knowledge graph: %s", exc)
            return {}
        finally:
            if conn:
                conn.close()

    def _export_config(self, output_path: str) -> int:
        """Export config to JSON file."""
        data = self._export_config_to_json()
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return 1

    def _export_config_to_json(self) -> Dict[str, Any]:
        """Export config as JSON dict（统一库 settings 表快照）."""
        try:
            from core.config_store import get_config_store
            snap = get_config_store(str(self._db_path)).snapshot()
            return snap or {}
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
        # v2.1.0 数据统一：所有业务表都在统一库
        self._db_path = self._data_dir / "one_agent.db"

    def import_from_file(
        self,
        file_path: str,
        merge: bool = True,
        restore_db: bool = False,
    ) -> ImportResult:
        """Import data from a backup file.

        Args:
            file_path: backup archive (.zip / .json).
            merge: json 数据增量合并（False = 配置整体替换）。
            restore_db: 归档含 one_agent.db(.enc) 时，直接还原整个统一库
                文件（新环境部署路径）。执行前会释放进程内 Hub/ConfigStore
                单例连接并 checkpoint 现库，现有库另存为
                ``one_agent.db.pre_import`` 兜底。
        """
        start_time = time.time()
        items_imported: Dict[str, int] = {}

        try:
            path = Path(file_path)
            if path.suffix == ".zip":
                items_imported = self._import_zip(file_path, merge, restore_db)
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

    def _import_zip(self, file_path: str, merge: bool, restore_db: bool = False) -> Dict[str, int]:
        """Import from ZIP archive."""
        items_imported: Dict[str, int] = {}

        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()

            # v2.2.0：整库还原（新环境部署）。加密条目需 ONE_AGENT_DB_KEY。
            if restore_db and ("one_agent.db" in names or "one_agent.db.enc" in names):
                items_imported["database_restored"] = self._restore_database(zf, names)

            for name in names:
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

    def _restore_database(self, zf: zipfile.ZipFile, names: List[str]) -> int:
        """把归档内的统一库文件原子还原到 data_dir（含加密解包）。"""
        enc_name = "one_agent.db.enc" in names
        if enc_name:
            from core.db_maintenance import decrypt_blob, get_db_passphrase

            passphrase = get_db_passphrase()
            if not passphrase:
                raise ValueError(
                    "备份已加密但未设置 ONE_AGENT_DB_KEY，无法还原统一库")
            blob = decrypt_blob(zf.read("one_agent.db.enc"), passphrase)
        else:
            blob = zf.read("one_agent.db")

        # 释放进程内单例连接（CLI 进程通常没有；防御 API 场景误调用）
        try:
            from core.hub import close_hub
            close_hub(str(self._data_dir))
        except Exception as exc:  # noqa: BLE001
            logger.debug("close hub before restore failed: %s", exc)
        try:
            from core.config_store import close_config_store
            close_config_store(str(self._db_path))
        except Exception as exc:  # noqa: BLE001
            logger.debug("close config store before restore failed: %s", exc)

        # 现库 WAL 落盘后另存兜底，再原子替换
        if self._db_path.exists():
            try:
                from core.hub import get_hub
                get_hub(str(self._data_dir)).checkpoint()
                close_hub(str(self._data_dir))
            except Exception as exc:  # noqa: BLE001
                logger.debug("pre-restore checkpoint failed: %s", exc)
            backup = self._db_path.with_name(self._db_path.name + ".pre_import")
            self._db_path.replace(backup)
            logger.info("existing database kept aside: %s", backup)

        tmp = self._db_path.with_name(self._db_path.name + ".restore_tmp")
        tmp.write_bytes(blob)
        for suffix in ("-wal", "-shm"):
            Path(str(self._db_path) + suffix).unlink(missing_ok=True)
        tmp.replace(self._db_path)
        try:
            os.chmod(self._db_path, 0o600)
        except OSError:
            pass
        return 1

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
        """Import sessions into the unified database."""
        if not data.get("sessions"):
            return 0

        self._ensure_tables("sessions")

        count = 0
        conn = None
        try:
            conn = create_sqlite_connection(str(self._db_path))
            for session in data["sessions"]:
                # v2.1.0：sessions 表主键列为 id（旧导出文件里叫 session_id）
                conn.execute(
                    "INSERT OR IGNORE INTO sessions"
                    "(id, title, created_at, updated_at, message_count, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (session.get("session_id") or session.get("id"),
                     session.get("title", ""),
                     session.get("created_at", 0),
                     session.get("updated_at", 0),
                     session.get("message_count", 0),
                     session.get("status", "active")),
                )
                count += 1
            conn.commit()
        except Exception as exc:
            logger.warning("Failed to import sessions: %s", exc)
        finally:
            if conn:
                conn.close()

        return count

    def _import_memory(self, data: Dict, merge: bool) -> int:
        """Import memory into the unified database (FTS memory 表)."""
        if not data.get("entries"):
            return 0

        self._ensure_tables("memory")

        count = 0
        conn = None
        try:
            conn = create_sqlite_connection(str(self._db_path))
            for entry in data["entries"]:
                conn.execute(
                    "INSERT INTO memory(content, source, tags, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (entry.get("text", ""), entry.get("source", "import"),
                     entry.get("tags", ""), entry.get("created_at", 0)),
                )
                count += 1
            conn.commit()
        except Exception as exc:
            logger.warning("Failed to import memory: %s", exc)
        finally:
            if conn:
                conn.close()

        return count

    def _import_kg(self, data: Dict, merge: bool) -> int:
        """Import knowledge graph into the unified database."""
        if not data.get("entities"):
            return 0

        self._ensure_tables("kg")

        count = 0
        conn = None
        try:
            conn = create_sqlite_connection(str(self._db_path))
            # name → id 映射（含本次新导入的实体），供 relations 解析
            name_to_id = {}
            for entity in data["entities"]:
                conn.execute(
                    "INSERT OR IGNORE INTO entities(name, type, created_at) "
                    "VALUES (?, ?, ?)",
                    (entity["name"], entity.get("entity_type", "unknown"),
                     entity.get("created_at", 0)),
                )
                row = conn.execute(
                    "SELECT id FROM entities WHERE name = ?", (entity["name"],)).fetchone()
                if row:
                    name_to_id[entity["name"]] = row["id"]
                count += 1
            for rel in data.get("relations", []):
                s_id = name_to_id.get(rel.get("subject_name"))
                o_id = name_to_id.get(rel.get("object_name"))
                if s_id is None or o_id is None:
                    continue
                conn.execute(
                    "INSERT INTO relations(subject_id, predicate, object_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (s_id, rel.get("predicate", ""), o_id, 0),
                )
            conn.commit()
        except Exception as exc:
            logger.warning("Failed to import knowledge graph: %s", exc)
        finally:
            if conn:
                conn.close()

        return count

    def _ensure_tables(self, kind: str) -> None:
        """导入前确保目标表结构就位（新环境空库可直接导入）。

        利用各 store 的构造即建表特性；任何失败仅记录（后续 SQL 会
        以 warning 失败并跳过，不会中断整个导入）。
        """
        db = str(self._db_path)
        try:
            if kind == "sessions":
                from memory.session_store import SessionStore
                store = SessionStore(db)
                getattr(store, "close", lambda: None)()
            elif kind == "memory":
                from memory import LongTermMemory
                LongTermMemory(path=db)
            elif kind == "kg":
                from memory.knowledge_graph import KnowledgeGraph
                KnowledgeGraph(db)
        except Exception as exc:
            logger.debug("ensure %s tables failed: %s", kind, exc)

    def _import_config(self, data: Dict, merge: bool) -> int:
        """Import config into the unified database (settings 表).

        v2.1.0：不再写 YAML 文件 —— 配置的事实源是统一库。
        """
        try:
            from core.config_store import get_config_store
            store = get_config_store(str(self._db_path))
            if merge:
                store.apply_updates(data)
            else:
                store.apply_full(data)
            return 1
        except Exception as exc:
            logger.warning("Failed to import config: %s", exc)
            return 0
