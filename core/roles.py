"""角色系统 — 预定义一组 LLM 行为模板，用户可通过 /role 切换。

每个角色由一个 "system prompt" 描述其身份、风格和目标。实现上：
- RoleLibrary 从 data/roles/prompts-zh.json 加载角色；
- build_role_prompt() 根据当前选中的角色生成 system 提示文本，
  由 router 和 coordinator 注入到消息列表的最前面；
- /role list / /role search / /role <名称> / /role off 由 coordinator
  的 SLASH_COMMANDS 提供支持。
"""

from __future__ import annotations

import json
import logging
import os
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_LIBRARY_PATH = "data/roles/prompts-zh.json"


class RoleLibrary:
    """加载并管理角色库的单例式容器。"""

    def __init__(self) -> None:
        self._roles: List[Dict[str, Any]] = []
        self._current: Optional[str] = None  # 当前角色名
        self._loaded = False

    # ------------------------------------------------------------ load / list

    def load(self, library_path: str = DEFAULT_LIBRARY_PATH) -> bool:
        """尝试从 JSON 文件加载角色库。"""
        path = library_path or DEFAULT_LIBRARY_PATH
        try:
            if not os.path.exists(path):
                logger.warning("role library not found: %s", path)
                self._roles = []
                return False
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 支持 { "roles": [...] } 格式，也支持纯列表
            if isinstance(data, dict) and isinstance(data.get("roles"), list):
                self._roles = [r for r in data["roles"] if isinstance(r, dict)]
            elif isinstance(data, list):
                self._roles = [r for r in data if isinstance(r, dict)]
            else:
                logger.warning("role library: unexpected format in %s", path)
                self._roles = []
                return False
            self._loaded = True
            logger.info("role library loaded: %d roles from %s", len(self._roles), path)
            return True
        except json.JSONDecodeError as exc:
            logger.warning("role library JSON decode error: %s", exc)
            self._roles = []
            return False
        except OSError as exc:
            logger.warning("role library read error: %s", exc)
            self._roles = []
            return False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def all(self) -> List[Dict[str, Any]]:
        """返回所有角色的副本，避免外部修改内部状态。"""
        return [dict(r) for r in self._roles]

    # ------------------------------------------------------------ lookup

    def find(self, name: str) -> Optional[Dict[str, str]]:
        """模糊匹配角色名，返回最匹配的角色（阈值 0.45）。"""
        if not name or not self._roles:
            return None
        q = name.strip().lower()
        best: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for role in self._roles:
            rname = str(role.get("name") or "")
            aliases = [rname] + [str(a) for a in (role.get("aliases") or [])]
            for cand in aliases:
                score = SequenceMatcher(None, q, cand.lower()).ratio()
                if cand.lower() == q:
                    score = 1.0
                if score > best_score:
                    best_score = score
                    best = role
        if best and best_score >= 0.45:
            return {
                "name": str(best.get("name") or ""),
                "prompt": str(best.get("prompt") or ""),
                "description": str(best.get("description") or ""),
            }
        return None

    # ------------------------------------------------------------ selection

    def select(self, name: str) -> Optional[str]:
        """选择角色（会先调用 find 做模糊匹配），返回最终角色名。"""
        match = self.find(name)
        if match:
            self._current = match["name"]
            return match["name"]
        return None

    def off(self) -> None:
        """关闭角色系统，回到默认助手行为。"""
        self._current = None

    def current(self) -> Optional[str]:
        return self._current

    def current_prompt(self) -> Optional[str]:
        if not self._current:
            return None
        match = self.find(self._current)
        return match["prompt"] if match else None


# ----------------------------------------------------------- module helpers


def build_role_prompt(library: RoleLibrary) -> str:
    """构造供 LLM system 前缀使用的角色提示文本。未选角色时返回空串。"""
    if library is None:
        return ""
    name = library.current()
    if not name:
        return ""
    match = library.find(name)
    if not match:
        return ""
    body = match.get("prompt") or ""
    if not body:
        return ""
    # 用一段清晰的前缀包裹角色描述，让 LLM 知道这是角色指令，且优先级高于
    # 后续通用系统指令。
    return (
        f"【当前角色：{name}】\n"
        "请你以以下角色身份与用户对话，并保持该角色的语气、价值观和约束。"
        "如果该角色的描述与用户的实际问题冲突，优先满足用户需求，但尽量在不"
        "违背安全原则的前提下贴合角色设定。\n\n"
        f"{body}\n"
    )


# 模块级单例 — 由 one_agent.py / coordinator.py 共享使用
role_library = RoleLibrary()
