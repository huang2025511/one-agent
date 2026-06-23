"""提示词模板管理 — 支持变量替换的模板系统。

模板存储在 JSON 文件中，支持增删改查、分类过滤、关键词搜索和变量渲染。
首次初始化时写入 5 个内置默认模板（代码审查、文本总结、翻译助手、邮件撰写、Bug 报告）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 变量占位符正则：匹配 {{variable_name}}
_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")

# 内置默认模板（首次初始化时写入）
_DEFAULT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "code_review",
        "name": "代码审查",
        "category": "development",
        "template": "请审查以下代码，关注：\n1. 安全性\n2. 性能\n3. 可读性\n\n```\n{{code}}\n```",
        "variables": ["code"],
        "description": "对代码进行全面审查",
        "created_at": 1719000000,
        "updated_at": 1719000000,
    },
    {
        "id": "summarize",
        "name": "文本总结",
        "category": "writing",
        "template": "请总结以下文本的要点，用简洁清晰的语言表达：\n\n{{text}}\n\n总结要求：\n- 突出核心观点\n- 语言精炼",
        "variables": ["text"],
        "description": "总结文本要点",
        "created_at": 1719000000,
        "updated_at": 1719000000,
    },
    {
        "id": "translate",
        "name": "翻译助手",
        "category": "writing",
        "template": "请将以下文本从 {{source_lang}} 翻译为 {{target_lang}}，保持原文的语气和风格：\n\n{{text}}",
        "variables": ["source_lang", "target_lang", "text"],
        "description": "多语言翻译",
        "created_at": 1719000000,
        "updated_at": 1719000000,
    },
    {
        "id": "email_writer",
        "name": "邮件撰写",
        "category": "writing",
        "template": "请帮我撰写一封{{tone}}语气的邮件。\n\n收件人：{{recipient}}\n主题：{{subject}}\n要点：{{points}}\n\n要求：专业、简洁、礼貌。",
        "variables": ["tone", "recipient", "subject", "points"],
        "description": "撰写专业邮件",
        "created_at": 1719000000,
        "updated_at": 1719000000,
    },
    {
        "id": "bug_report",
        "name": "Bug 报告生成",
        "category": "development",
        "template": "请根据以下信息生成一份规范的 Bug 报告：\n\n问题描述：{{description}}\n复现步骤：{{steps}}\n预期行为：{{expected}}\n实际行为：{{actual}}\n环境信息：{{environment}}",
        "variables": ["description", "steps", "expected", "actual", "environment"],
        "description": "生成标准化 Bug 报告",
        "created_at": 1719000000,
        "updated_at": 1719000000,
    },
]


class PromptTemplateManager:
    """提示词模板管理 — 支持变量替换的模板系统。

    模板存储在 data/prompt_templates/templates.json，格式：
    [
        {
            "id": "code_review",
            "name": "代码审查",
            "category": "development",
            "template": "请审查以下代码，关注：\n1. 安全性\n2. 性能\n3. 可读性\n\n```\n{{code}}\n```",
            "variables": ["code"],
            "description": "对代码进行全面审查",
            "created_at": 1719000000,
            "updated_at": 1719000000
        }
    ]
    """

    def __init__(self, templates_dir: str = "data/prompt_templates"):
        self.templates_dir = templates_dir
        self.templates_file = os.path.join(templates_dir, "templates.json")
        self._templates: List[Dict[str, Any]] = []
        try:
            # 确保目录存在
            os.makedirs(self.templates_dir, exist_ok=True)
            # 加载或初始化模板
            self._load()
        except Exception as exc:
            logger.error("初始化模板管理器失败: %s", exc, exc_info=True)
            self._templates = []

    # ---------------------------------------------------------- 持久化
    def _load(self) -> None:
        """从 JSON 文件加载模板；文件不存在时写入默认模板。"""
        try:
            if not os.path.exists(self.templates_file):
                # 首次初始化：写入默认模板
                self._templates = [dict(t) for t in _DEFAULT_TEMPLATES]
                self._save()
                logger.info("已初始化 %d 个默认模板", len(self._templates))
                return
            with open(self.templates_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._templates = data
            else:
                # 文件格式错误：重置为默认模板
                logger.error("模板文件格式错误，应为列表，已重置为默认模板")
                self._templates = [dict(t) for t in _DEFAULT_TEMPLATES]
                self._save()
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("加载模板文件失败: %s", exc, exc_info=True)
            # 加载失败时回退到默认模板
            self._templates = [dict(t) for t in _DEFAULT_TEMPLATES]
            try:
                self._save()
            except Exception as exc2:
                logger.error("恢复默认模板失败: %s", exc2, exc_info=True)

    def _save(self) -> None:
        """将模板持久化到 JSON 文件。"""
        try:
            with open(self.templates_file, "w", encoding="utf-8") as f:
                json.dump(self._templates, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("保存模板文件失败: %s", exc, exc_info=True)

    # ---------------------------------------------------------- 内部工具
    @staticmethod
    def _extract_variables(template: str) -> List[str]:
        """从模板字符串中提取 {{variable}} 占位符的变量名（去重保序）。"""
        seen = set()
        result = []
        for match in _VAR_PATTERN.finditer(template or ""):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                result.append(name)
        return result

    def _generate_id(self, name: str) -> str:
        """根据名称生成唯一 ID；冲突时追加 _2、_3 后缀。"""
        base = (name or "").lower().replace(" ", "_")
        if not base:
            base = "template"
        candidate = base
        suffix = 2
        existing_ids = {t.get("id") for t in self._templates}
        while candidate in existing_ids:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    # ---------------------------------------------------------- 公共 API
    def list_templates(self, category: str = None) -> List[Dict[str, Any]]:
        """列出所有模板，可按分类过滤。"""
        try:
            if category is None:
                return [dict(t) for t in self._templates]
            return [dict(t) for t in self._templates if t.get("category") == category]
        except Exception as exc:
            logger.error("列出模板失败: %s", exc, exc_info=True)
            return []

    def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """获取单个模板。"""
        try:
            for t in self._templates:
                if t.get("id") == template_id:
                    return dict(t)
            return None
        except Exception as exc:
            logger.error("获取模板失败: %s", exc, exc_info=True)
            return None

    def add_template(self, name: str, template: str, category: str = "general",
                     description: str = "", variables: List[str] = None) -> Dict[str, Any]:
        """添加新模板。自动提取 {{variable}} 并生成 ID。返回新模板。"""
        try:
            now = time.time()
            # 如果未显式提供 variables，则从模板中自动提取
            if variables is None:
                variables = self._extract_variables(template)
            new_template = {
                "id": self._generate_id(name),
                "name": name,
                "category": category,
                "template": template,
                "variables": list(variables),
                "description": description,
                "created_at": now,
                "updated_at": now,
            }
            self._templates.append(new_template)
            self._save()
            logger.info("已添加模板: %s (%s)", new_template["id"], name)
            return dict(new_template)
        except Exception as exc:
            logger.error("添加模板失败: %s", exc, exc_info=True)
            return {}

    def update_template(self, template_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """更新模板。支持 name/template/category/description/variables 字段。"""
        try:
            for t in self._templates:
                if t.get("id") == template_id:
                    # 更新允许的字段
                    if "name" in kwargs:
                        t["name"] = kwargs["name"]
                    if "category" in kwargs:
                        t["category"] = kwargs["category"]
                    if "description" in kwargs:
                        t["description"] = kwargs["description"]
                    if "template" in kwargs:
                        t["template"] = kwargs["template"]
                        # 模板变更后若未显式提供 variables，则自动重新提取
                        if "variables" not in kwargs:
                            t["variables"] = self._extract_variables(t["template"])
                    if "variables" in kwargs:
                        t["variables"] = list(kwargs["variables"])
                    t["updated_at"] = time.time()
                    self._save()
                    logger.info("已更新模板: %s", template_id)
                    return dict(t)
            logger.warning("更新模板失败：未找到模板 %s", template_id)
            return None
        except Exception as exc:
            logger.error("更新模板失败: %s", exc, exc_info=True)
            return None

    def delete_template(self, template_id: str) -> bool:
        """删除模板。"""
        try:
            for i, t in enumerate(self._templates):
                if t.get("id") == template_id:
                    self._templates.pop(i)
                    self._save()
                    logger.info("已删除模板: %s", template_id)
                    return True
            logger.warning("删除模板失败：未找到模板 %s", template_id)
            return False
        except Exception as exc:
            logger.error("删除模板失败: %s", exc, exc_info=True)
            return False

    def render(self, template_id: str, variables: Dict[str, str] = None) -> Optional[str]:
        """渲染模板，替换 {{variable}} 占位符。未提供的变量替换为空字符串。"""
        try:
            template = None
            for t in self._templates:
                if t.get("id") == template_id:
                    template = t.get("template", "")
                    break
            if template is None:
                logger.warning("渲染失败：未找到模板 %s", template_id)
                return None
            variables = variables or {}

            def _replace(match: "re.Match") -> str:
                # 未提供的变量替换为空字符串
                return str(variables.get(match.group(1), ""))

            return _VAR_PATTERN.sub(_replace, template)
        except Exception as exc:
            logger.error("渲染模板失败: %s", exc, exc_info=True)
            return None

    def list_categories(self) -> List[str]:
        """列出所有分类。"""
        try:
            seen = set()
            result = []
            for t in self._templates:
                cat = t.get("category")
                if cat and cat not in seen:
                    seen.add(cat)
                    result.append(cat)
            return result
        except Exception as exc:
            logger.error("列出分类失败: %s", exc, exc_info=True)
            return []

    def search(self, keyword: str) -> List[Dict[str, Any]]:
        """按关键词搜索模板（name/description/template 字段）。"""
        try:
            if not keyword:
                return []
            kw = keyword.lower()
            result = []
            for t in self._templates:
                hay = " ".join([
                    str(t.get("name", "")),
                    str(t.get("description", "")),
                    str(t.get("template", "")),
                ]).lower()
                if kw in hay:
                    result.append(dict(t))
            return result
        except Exception as exc:
            logger.error("搜索模板失败: %s", exc, exc_info=True)
            return []


__all__ = ["PromptTemplateManager"]
