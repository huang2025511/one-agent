"""SQLite-backed configuration store — 配置的单一事实源。

架构（v2.1.0 数据统一）：
    代码默认值 (pydantic FullConfig)
      < YAML 文件（仅首次启动作种子，之后不再读改）
      < one_agent.db settings 表覆盖（与其他系统状态同库，运行时唯一可写配置源）
      < 环境变量展开（${VAR} 在合并后仍会展开）

设计要点：
  - 按顶层段（agent/llm/router/...）整段存储 JSON，写入前过 pydantic
    校验，坏配置在入口被拒绝而不是留到下次启动炸掉。
  - 首次启动 seed_if_empty() 把 YAML 全量导入 DB；此后所有修改
    （PUT /api/config、config set 技能、_save_config）只写 DB，
    YAML 文件不再被写——环境迁移 = 拷贝 data/ 目录。
  - DB 文件权限 0600，位于数据目录内，与聊天记录同等敏感级别，
    因此存明文（旧的 YAML 脱敏写回会导致重启后 API key 退化成
    ${VAR} 占位符，属于实际 bug）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.db import create_sqlite_connection
from core.hub import database_path  # noqa: F401  (统一库)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    section     TEXT PRIMARY KEY,   -- 顶层段名：agent / llm / router / ...
    payload     TEXT NOT NULL,      -- 该段完整 JSON
    updated_at  REAL NOT NULL
)
"""

# 进程内单例：同一 DB 路径共享连接（SQLite 连接跨线程由调用方锁保护）
_instances: Dict[str, "ConfigStore"] = {}
_instances_lock = threading.Lock()


def resolve_data_dir(cfg: Optional[Dict[str, Any]] = None) -> str:
    """解析数据目录：ONE_AGENT_DATA_DIR 环境变量 > agent.data_dir > ./data。

    环境变量优先保证测试/多实例可强制隔离。实现移至 core.hub（配置
    加载前中枢也要用它定位统一库），此处保留转发。
    """
    from core.hub import resolve_data_dir as _resolve
    return _resolve(cfg)


def config_db_path(data_dir: str) -> Path:
    """配置所在的统一库路径（v2.1.0 起配置与其他状态同库）。"""
    return Path(database_path(data_dir))


def overlay_enabled(config_path: Optional[str] = None) -> bool:
    """DB 覆盖层是否生效。

    规则：加载的 yaml 位于项目 config/ 目录（部署配置：default/
    dev/prod/test_config.yaml）→ 叠加 DB、允许种子导入。
    其他位置（单测自建 yaml 的惯用法）→ 该文件完全接管配置，
    不叠加 DB、不做种子导入，测试间互不串扰。
    """
    if not config_path:
        return True
    try:
        cfg_dir = (Path(__file__).resolve().parent.parent / "config").resolve()
        return Path(config_path).resolve().parent == cfg_dir
    except Exception:
        return True


def get_config_store(db_path: str) -> "ConfigStore":
    """获取（或创建）指向 db_path 的进程级单例 ConfigStore。"""
    key = str(Path(db_path).absolute())
    with _instances_lock:
        st = _instances.get(key)
        if st is None:
            st = ConfigStore(db_path)
            _instances[key] = st
        return st


def close_config_store(db_path: Optional[str] = None) -> None:
    """关闭单例（测试用）：无参关闭全部。"""
    with _instances_lock:
        targets = list(_instances.items()) if db_path is None else [
            (k, v) for k, v in _instances.items()
            if k == str(Path(db_path).absolute())
        ]
        for k, v in targets:
            try:
                v.close()
            except Exception:
                pass
            _instances.pop(k, None)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _validate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """用 FullConfig 校验并规范化；非法配置抛 ValueError。

    延迟导入避免 one_agent <-> core.config_store 循环依赖。
    """
    from one_agent import FullConfig
    try:
        return FullConfig(**cfg).model_dump()
    except Exception as exc:  # pydantic ValidationError 等
        raise ValueError(f"invalid config: {exc}") from exc


class ConfigStore:
    """按段存储的配置库。线程安全（进程内单例 + RLock）。"""

    def __init__(self, db_path: str) -> None:
        self.path = str(db_path)
        self._lock = threading.RLock()
        self._conn = create_sqlite_connection(self.path)
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- 读 -------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """全部已存储段合并为嵌套 dict；空库返回 {}。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT section, payload FROM settings").fetchall()
        out: Dict[str, Any] = {}
        for r in rows:
            try:
                out[r["section"]] = json.loads(r["payload"])
            except (ValueError, TypeError):
                logger.warning("config db: skipping corrupt section %s", r["section"])
        return out

    def is_empty(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
            return row is None

    # -- 写 -------------------------------------------------------------

    def seed_if_empty(self, cfg: Dict[str, Any]) -> bool:
        """库为空时导入完整快照（首次启动从 YAML 迁移）。返回是否写入。"""
        with self._lock:
            if not self.is_empty():
                return False
            self._write_all(cfg)
            return True

    def apply_full(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """整体替换存储内容（先校验）。返回规范化后的 dict。"""
        normalized = _validate(cfg)
        with self._lock:
            self._write_all(normalized)
        return normalized

    def apply_updates(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """增量合并到当前快照后整体写入（先校验）。"""
        with self._lock:
            merged = _deep_merge(self.snapshot(), updates)
        return self.apply_full(merged)

    def _write_all(self, cfg: Dict[str, Any]) -> None:
        """调用方需持有 self._lock。单事务整体替换。"""
        now = time.time()
        with self._conn:
            self._conn.execute("DELETE FROM settings")
            for section in sorted(cfg.keys()):
                self._conn.execute(
                    "INSERT INTO settings(section, payload, updated_at) VALUES (?,?,?)",
                    (section, json.dumps(cfg[section], ensure_ascii=False), now),
                )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ============================================================
# 供外部调用的便捷入口（自动定位 DB 路径）
# ============================================================

def _store_for(cfg: Dict[str, Any]) -> ConfigStore:
    return get_config_store(str(config_db_path(resolve_data_dir(cfg))))


def save_config_dict(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """持久化完整配置 dict 到配置库（校验失败抛 ValueError）。"""
    return _store_for(cfg).apply_full(cfg)


def seed_from_config(config_obj: Any) -> bool:
    """用 FullConfig 对象做首次种子导入（OneAgentApp 启动时调用）。"""
    cfg = config_obj.model_dump() if hasattr(config_obj, "model_dump") else dict(config_obj)
    return _store_for(cfg).seed_if_empty(cfg)
