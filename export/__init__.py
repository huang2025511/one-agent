"""数据导出模块 — 支持会话历史、长期记忆、配置、审计日志的导出。

导出文件统一保存到 ``data/exports/`` 目录，文件名格式为
``{type}_{timestamp}.{ext}``（如 ``sessions_20260623_153000.json``）。
"""

import json
import logging
import os
import zipfile
from datetime import datetime

logger = logging.getLogger(__name__)

# 尝试导入依赖模块；缺失时对应的导出方法将返回空字符串
try:
    from memory.session_store import SessionStore
    _HAS_SESSION_STORE = True
except Exception as exc:  # pragma: no cover - 依赖缺失时的降级路径
    logger.warning("无法导入 SessionStore: %s", exc)
    SessionStore = None
    _HAS_SESSION_STORE = False

try:
    from memory import LongTermMemory
    _HAS_LONG_TERM_MEMORY = True
except Exception as exc:  # pragma: no cover - 依赖缺失时的降级路径
    logger.warning("无法导入 LongTermMemory: %s", exc)
    LongTermMemory = None
    _HAS_LONG_TERM_MEMORY = False

try:
    from config_backup import ConfigBackupManager
    _HAS_CONFIG_BACKUP = True
except Exception as exc:  # pragma: no cover - 依赖缺失时的降级路径
    logger.warning("无法导入 ConfigBackupManager: %s", exc)
    ConfigBackupManager = None
    _HAS_CONFIG_BACKUP = False

try:
    from core.audit_log import AuditLog
    _HAS_AUDIT_LOG = True
except Exception as exc:  # pragma: no cover - 依赖缺失时的降级路径
    logger.warning("无法导入 AuditLog: %s", exc)
    AuditLog = None
    _HAS_AUDIT_LOG = False


class DataExporter:
    """数据导出工具 — 支持会话、记忆、配置、审计日志的导出。"""

    def __init__(self, config_path: str = "config/default_config.yaml",
                 data_dir: str = "data"):
        self._config_path = config_path
        self._data_dir = data_dir
        # 导出文件保存目录（自动创建）
        self._exports_dir = os.path.join(data_dir, "exports")
        os.makedirs(self._exports_dir, exist_ok=True)

    # -------------------------------------------------------- 内部工具

    def _timestamp(self) -> str:
        """生成时间戳字符串，格式 YYYYMMDD_HHMMSS。"""
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    @staticmethod
    def _write_file(filepath: str, content: str) -> None:
        """将字符串内容以 UTF-8 编码写入文件。"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    # -------------------------------------------------------- 公开接口

    def export_sessions(self, format: str = "json") -> str:
        """导出所有会话历史。format: json | markdown。返回文件路径。"""
        if not _HAS_SESSION_STORE:
            logger.error("SessionStore 模块不可用，无法导出会话")
            return ""
        store = None
        try:
            # 初始化会话存储（数据库路径基于 data_dir）
            db_path = os.path.join(self._data_dir, "memory", "sessions.db")
            store = SessionStore(db_path)

            # 获取所有会话列表（使用较大的 limit 尽量取全）
            sessions = store.list_sessions(limit=100000, offset=0)

            # 逐个会话获取完整信息（含消息列表）
            all_sessions = []
            for session in sessions:
                session_id = session.get("id")
                if not session_id:
                    continue
                full_session = store.get_session(session_id)
                if full_session:
                    all_sessions.append(full_session)

            # 生成文件名与路径
            ext = "json" if format == "json" else "md"
            filename = f"sessions_{self._timestamp()}.{ext}"
            filepath = os.path.join(self._exports_dir, filename)

            if format == "json":
                # JSON 格式：美化输出
                content = json.dumps(all_sessions, indent=2, ensure_ascii=False)
            else:
                # Markdown 格式：每个会话标题 + 每条消息
                lines = []
                for session in all_sessions:
                    title = session.get("title", "未命名会话")
                    lines.append(f"## 会话: {title}\n\n")
                    for msg in session.get("messages", []):
                        role = msg.get("role", "unknown")
                        msg_content = msg.get("content", "")
                        lines.append(f"**{role}**: {msg_content}\n\n")
                content = "".join(lines)

            self._write_file(filepath, content)
            logger.info("会话导出成功: %s（共 %d 个会话）", filepath, len(all_sessions))
            return filepath
        except Exception as exc:
            logger.error("导出会话失败: %s", exc, exc_info=True)
            return ""
        finally:
            # 关闭数据库连接，避免资源泄漏
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    def export_memory(self, format: str = "json", limit: int = 1000) -> str:
        """导出长期记忆。format: json | markdown。返回文件路径。"""
        if not _HAS_LONG_TERM_MEMORY:
            logger.error("LongTermMemory 模块不可用，无法导出记忆")
            return ""
        memory = None
        try:
            # 初始化长期记忆存储
            db_path = os.path.join(self._data_dir, "memory", "longterm.sqlite")
            memory = LongTermMemory(path=db_path)

            # 分页获取所有记忆条目，直到达到 limit 或取完全部数据
            all_items = []
            page = 1
            page_size = 100
            while len(all_items) < limit:
                result = memory.paginate(page=page, page_size=page_size)
                items = result.get("items", [])
                if not items:
                    break
                all_items.extend(items)
                # 已获取全部数据则停止
                total = result.get("total", 0)
                if len(all_items) >= total:
                    break
                page += 1

            # 截断到指定数量
            all_items = all_items[:limit]

            # 生成文件名与路径
            ext = "json" if format == "json" else "md"
            filename = f"memory_{self._timestamp()}.{ext}"
            filepath = os.path.join(self._exports_dir, filename)

            if format == "json":
                # JSON 格式：美化输出
                content = json.dumps(all_items, indent=2, ensure_ascii=False)
            else:
                # Markdown 格式：每条记忆含 ID、正文、来源与标签
                lines = []
                for idx, item in enumerate(all_items, 1):
                    text = item.get("content", "")
                    source = item.get("source", "")
                    tags = item.get("tags", "")
                    item_id = item.get("id", idx)
                    lines.append(
                        f"## 记忆 #{item_id}\n\n{text}\n\n"
                        f"> 来源: {source} | 标签: {tags}\n\n"
                    )
                content = "".join(lines)

            self._write_file(filepath, content)
            logger.info("记忆导出成功: %s（共 %d 条）", filepath, len(all_items))
            return filepath
        except Exception as exc:
            logger.error("导出记忆失败: %s", exc, exc_info=True)
            return ""
        finally:
            if memory is not None:
                try:
                    memory.close()
                except Exception:
                    pass

    def export_config(self) -> str:
        """导出当前配置（YAML 格式）。返回文件路径。"""
        if not _HAS_CONFIG_BACKUP:
            logger.error("ConfigBackupManager 模块不可用，无法导出配置")
            return ""
        try:
            # 读取当前配置文件内容（配置本身已是 YAML 格式，直接导出）
            if not os.path.exists(self._config_path):
                logger.error("配置文件不存在: %s", self._config_path)
                return ""

            with open(self._config_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 生成文件名与路径
            filename = f"config_{self._timestamp()}.yaml"
            filepath = os.path.join(self._exports_dir, filename)

            self._write_file(filepath, content)
            logger.info("配置导出成功: %s", filepath)
            return filepath
        except Exception as exc:
            logger.error("导出配置失败: %s", exc, exc_info=True)
            return ""

    def export_audit_log(self, limit: int = 500) -> str:
        """导出审计日志（JSON 格式）。返回文件路径。"""
        if not _HAS_AUDIT_LOG:
            logger.error("AuditLog 模块不可用，无法导出审计日志")
            return ""
        audit = None
        try:
            # 初始化审计日志存储
            db_path = os.path.join(self._data_dir, "memory", "audit.db")
            audit = AuditLog(db_path)

            # 查询最近的审计日志条目
            entries = audit.query(limit=limit)

            # 生成文件名与路径
            filename = f"audit_log_{self._timestamp()}.json"
            filepath = os.path.join(self._exports_dir, filename)

            # JSON 格式：美化输出
            content = json.dumps(entries, indent=2, ensure_ascii=False)
            self._write_file(filepath, content)
            logger.info("审计日志导出成功: %s（共 %d 条）", filepath, len(entries))
            return filepath
        except Exception as exc:
            logger.error("导出审计日志失败: %s", exc, exc_info=True)
            return ""
        finally:
            if audit is not None:
                try:
                    audit.close()
                except Exception:
                    pass

    def export_all(self) -> str:
        """导出所有数据到单个 ZIP 文件。返回 ZIP 文件路径。"""
        try:
            # 分别导出各类数据，收集成功导出的文件路径
            exported_files = []
            for path in (
                self.export_sessions(format="json"),
                self.export_memory(format="json"),
                self.export_config(),
                self.export_audit_log(),
            ):
                if path:
                    exported_files.append(path)

            if not exported_files:
                logger.error("没有可导出的数据")
                return ""

            # 将所有导出文件打包成 ZIP
            zip_filename = f"export_all_{self._timestamp()}.zip"
            zip_filepath = os.path.join(self._exports_dir, zip_filename)

            with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zf:
                for filepath in exported_files:
                    if os.path.exists(filepath):
                        # 压缩包内使用文件名作为路径
                        arcname = os.path.basename(filepath)
                        zf.write(filepath, arcname)

            logger.info("全部数据导出成功: %s（共 %d 个文件）",
                        zip_filepath, len(exported_files))
            return zip_filepath
        except Exception as exc:
            logger.error("导出全部数据失败: %s", exc, exc_info=True)
            return ""
