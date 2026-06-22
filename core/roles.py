"""角色系统 — 让 Agent 能扮演不同行业/场景的专家角色。

角色库来自开源项目 awesome-chatgpt-prompts-zh（CC0-1.0），
包含 124 个中文角色提示词，覆盖编程、写作、教育、翻译、咨询等场景。

用法：
    /role              — 查看当前角色
    /role list         — 列出所有可用角色
    /role <角色名>      — 切换到指定角色
    /role off          — 关闭角色（恢复默认 One-Agent 身份）
    /role search <关键词> — 搜索角色

角色 prompt 会追加到默认 system prompt 之后，不替换核心铁律。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认角色库路径（相对于工作目录）
DEFAULT_LIBRARY_PATH = "data/roles/prompts-zh.json"


class RoleLibrary:
    """角色库 — 加载、查询、列出角色。

    惰性加载：首次访问时才读取 JSON 文件。
    """

    def __init__(self) -> None:
        self._roles: List[Dict[str, str]] = []
        self._loaded: bool = False
        self._library_path: Optional[Path] = None

    def load(self, library_path: str = DEFAULT_LIBRARY_PATH) -> bool:
        """从 JSON 文件加载角色库。

        Args:
            library_path: JSON 文件路径（相对于工作目录或绝对路径）

        Returns:
            True 如果加载成功，False 否则
        """
        path = Path(library_path)
        if not path.is_absolute():
            # 尝试相对于当前工作目录
            path = Path.cwd() / library_path

        if not path.exists():
            logger.warning("角色库文件不存在: %s", path)
            self._loaded = False
            return False

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._roles = [
                    {
                        "act": str(r.get("act", "")).strip(),
                        "prompt": str(r.get("prompt", "")).strip(),
                    }
                    for r in data
                    if r.get("act") and r.get("prompt")
                ]
            elif isinstance(data, dict):
                # 兼容 {角色名: prompt} 格式
                self._roles = [
                    {"act": k.strip(), "prompt": str(v).strip()}
                    for k, v in data.items()
                    if k and v
                ]
            else:
                logger.error("角色库格式不支持: %s", type(data))
                self._loaded = False
                return False

            self._library_path = path
            self._loaded = True
            logger.info("角色库加载成功: %d 个角色 (from %s)", len(self._roles), path)
            return True
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("角色库加载失败: %s", exc)
            self._loaded = False
            return False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def size(self) -> int:
        return len(self._roles)

    def list_all(self) -> List[Dict[str, str]]:
        """返回所有角色。"""
        if not self._loaded:
            self.load()
        return list(self._roles)

    def find(self, name: str) -> Optional[Dict[str, str]]:
        """精确查找角色（支持模糊匹配）。

        先尝试精确匹配，再尝试包含匹配，最后尝试去掉前缀匹配
        （如 "担任雅思写作考官" → "雅思写作考官"）。
        """
        if not self._loaded:
            self.load()
        if not self._roles:
            return None

        target = name.strip()
        # 1. 精确匹配
        for r in self._roles:
            if r["act"] == target:
                return r

        # 2. 去掉常见前缀后再精确匹配
        prefixes = ["担任", "充当", "作为", "扮演", "act as "]
        normalized_target = target
        for p in prefixes:
            if normalized_target.lower().startswith(p.lower()):
                normalized_target = normalized_target[len(p):].strip()
        for r in self._roles:
            normalized_act = r["act"]
            for p in prefixes:
                if normalized_act.lower().startswith(p.lower()):
                    normalized_act = normalized_act[len(p):].strip()
                    break
            if normalized_act == normalized_target:
                return r

        # 3. 包含匹配（target 是 act 的子串，或反过来）
        for r in self._roles:
            if target in r["act"] or r["act"] in target:
                return r

        return None

    def search(self, keyword: str, limit: int = 20) -> List[Dict[str, str]]:
        """搜索角色（关键词匹配 act 字段）。"""
        if not self._loaded:
            self.load()
        kw = keyword.strip().lower()
        if not kw:
            return []
        results = [
            r for r in self._roles
            if kw in r["act"].lower() or kw in r["prompt"].lower()
        ]
        return results[:limit]


# 模块级单例
_library = RoleLibrary()


def get_library() -> RoleLibrary:
    """获取全局角色库单例。"""
    return _library


def get_current_role(config: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """从配置中读取当前激活的角色。

    Args:
        config: 完整配置字典

    Returns:
        角色字典 {"act": ..., "prompt": ...}，如果未启用角色则返回 None
    """
    role_cfg = config.get("agent", {}).get("role", {}) or {}
    if not role_cfg.get("enabled", True):
        return None

    current_name = role_cfg.get("current", "").strip()
    if not current_name or current_name.lower() in ("off", "default", "none", "关闭", "默认"):
        return None

    # 确保库已加载
    library_path = role_cfg.get("library", DEFAULT_LIBRARY_PATH)
    if not _library.loaded:
        _library.load(library_path)

    return _library.find(current_name)


def build_role_prompt(config: Dict[str, Any]) -> str:
    """构建角色 prompt 片段（追加到默认 system prompt 之后）。

    如果当前没有激活角色，返回空字符串。
    """
    role = get_current_role(config)
    if role is None:
        return ""

    return (
        f"\n\n【当前角色 — 你现在扮演：{role['act']}】\n"
        f"{role['prompt']}\n"
        f"【角色说明结束 — 请在保持上述角色身份的同时，继续遵循 One-Agent 的核心铁律】\n"
    )
