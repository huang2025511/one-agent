"""Agent 模板市场 — 预配置Agent模板，一键创建特定领域Agent。

提供：
  - 内置多领域Agent模板
  - 模板版本管理
  - 用户自定义模板
  - 一键创建Agent实例
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class AgentTemplate:
    """Agent模板类。"""
    template_id: str
    name: str
    display_name: str
    description: str
    category: str
    icon: str = ""
    version: str = "1.0.0"
    system_prompt: str = ""
    role: str = ""
    skills: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    author: str = "system"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    usage_count: int = 0
    rating: float = 0.0


@dataclass
class TemplateCategory:
    """模板分类类。"""
    category_id: str
    name: str
    display_name: str
    description: str = ""
    icon: str = ""
    template_count: int = 0


class TemplateManager:
    """模板管理器 — 管理Agent模板的增删改查。"""

    BUILTIN_TEMPLATES = [
        {
            "name": "programming_assistant",
            "display_name": "编程助手",
            "description": "专业的编程助手，支持代码生成、调试、重构、代码解释等",
            "category": "development",
            "icon": "💻",
            "system_prompt": "你是一名资深的全栈开发工程师，精通多种编程语言和框架。你的任务是帮助用户编写高质量的代码，解答技术问题，进行代码审查和优化。请提供清晰、简洁、可运行的代码示例，并解释关键思路。",
            "role": "programmer",
            "skills": ["python_runner", "document_search"],
            "tags": ["编程", "代码", "开发", "Python", "JavaScript"],
        },
        {
            "name": "content_writer",
            "display_name": "文案写作",
            "description": "专业的文案写作助手，支持文章、广告、邮件、社交媒体等多种文体",
            "category": "writing",
            "icon": "✍️",
            "system_prompt": "你是一名资深的内容创作专家，擅长撰写各种类型的文案。你的任务是帮助用户创作高质量的内容，包括文章、广告文案、邮件、社交媒体帖子等。请根据用户需求调整风格，确保内容有吸引力、结构清晰、语言流畅。",
            "role": "writer",
            "skills": [],
            "tags": ["写作", "文案", "内容", "创作"],
        },
        {
            "name": "data_analyst",
            "display_name": "数据分析",
            "description": "专业的数据分析助手，支持数据清洗、可视化、统计分析等",
            "category": "data",
            "icon": "📊",
            "system_prompt": "你是一名资深的数据分析师，精通数据分析方法和工具。你的任务是帮助用户进行数据分析，包括数据清洗、探索性分析、统计建模、数据可视化等。请用清晰的方式解释分析结果，并给出可操作的建议。",
            "role": "analyst",
            "skills": ["python_runner"],
            "tags": ["数据分析", "统计", "可视化", "Python"],
        },
        {
            "name": "customer_service",
            "display_name": "客服助手",
            "description": "专业的客户服务助手，支持常见问题解答、投诉处理、产品咨询等",
            "category": "service",
            "icon": "🎧",
            "system_prompt": "你是一名专业的客户服务代表，热情、耐心、专业。你的任务是为客户提供优质的服务体验，解答疑问，处理投诉，提供产品信息。请始终保持礼貌和同理心，确保客户满意。",
            "role": "assistant",
            "skills": [],
            "tags": ["客服", "服务", "咨询", "支持"],
        },
        {
            "name": "language_tutor",
            "display_name": "语言学习",
            "description": "语言学习助手，支持多语言教学、对话练习、语法讲解等",
            "category": "education",
            "icon": "🌍",
            "system_prompt": "你是一名专业的语言教师，擅长多种语言的教学。你的任务是帮助用户学习语言，包括词汇、语法、口语练习、文化知识等。请根据用户的水平调整难度，提供有趣且实用的学习内容。",
            "role": "teacher",
            "skills": [],
            "tags": ["语言", "学习", "教育", "英语"],
        },
        {
            "name": "health_advisor",
            "display_name": "健康顾问",
            "description": "健康生活顾问，提供饮食、运动、作息等方面的建议",
            "category": "health",
            "icon": "💚",
            "system_prompt": "你是一名专业的健康生活顾问，关注用户的身心健康。你的任务是提供科学的健康建议，包括饮食营养、运动健身、作息规律、心理调节等方面。请注意：你不能替代专业医疗诊断，如有疾病请及时就医。",
            "role": "advisor",
            "skills": [],
            "tags": ["健康", "健身", "饮食", "生活"],
        },
        {
            "name": "product_manager",
            "display_name": "产品经理",
            "description": "产品管理助手，支持需求分析、产品设计、竞品分析等",
            "category": "business",
            "icon": "📋",
            "system_prompt": "你是一名资深的产品经理，擅长产品规划和设计。你的任务是帮助用户进行产品分析和设计，包括需求调研、用户画像、功能设计、竞品分析、产品路线图等。请提供结构化、可落地的建议。",
            "role": "pm",
            "skills": [],
            "tags": ["产品", "需求", "设计", "商业"],
        },
        {
            "name": "creative_designer",
            "display_name": "创意设计",
            "description": "创意设计助手，支持视觉设计、UI/UX、品牌设计等",
            "category": "design",
            "icon": "🎨",
            "system_prompt": "你是一名富有创意的设计师，擅长视觉设计和用户体验设计。你的任务是帮助用户进行创意设计，包括视觉设计、UI/UX设计、品牌设计、创意策划等。请提供新颖且实用的设计思路。",
            "role": "designer",
            "skills": [],
            "tags": ["设计", "创意", "UI", "视觉"],
        },
    ]

    BUILTIN_CATEGORIES = [
        {"category_id": "development", "name": "开发", "display_name": "开发工具", "description": "编程开发相关的Agent模板", "icon": "💻"},
        {"category_id": "writing", "name": "写作", "display_name": "内容创作", "description": "内容创作相关的Agent模板", "icon": "✍️"},
        {"category_id": "data", "name": "数据", "display_name": "数据分析", "description": "数据分析相关的Agent模板", "icon": "📊"},
        {"category_id": "service", "name": "服务", "display_name": "客户服务", "description": "客户服务相关的Agent模板", "icon": "🎧"},
        {"category_id": "education", "name": "教育", "display_name": "教育培训", "description": "教育学习相关的Agent模板", "icon": "🌍"},
        {"category_id": "health", "name": "健康", "display_name": "健康生活", "description": "健康生活相关的Agent模板", "icon": "💚"},
        {"category_id": "business", "name": "商业", "display_name": "商业管理", "description": "商业管理相关的Agent模板", "icon": "📋"},
        {"category_id": "design", "name": "设计", "display_name": "创意设计", "description": "创意设计相关的Agent模板", "icon": "🎨"},
    ]

    def __init__(self, data_dir: str = "data/agent_templates"):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._templates_file = self._data_dir / "templates.json"
        self._templates: Dict[str, AgentTemplate] = {}
        self._load_templates()

    def _load_templates(self):
        """加载模板（内置 + 自定义）。"""
        # 加载内置模板
        for tpl_data in self.BUILTIN_TEMPLATES:
            template = AgentTemplate(
                template_id=f"builtin_{tpl_data['name']}",
                name=tpl_data["name"],
                display_name=tpl_data["display_name"],
                description=tpl_data["description"],
                category=tpl_data["category"],
                icon=tpl_data.get("icon", ""),
                system_prompt=tpl_data.get("system_prompt", ""),
                role=tpl_data.get("role", ""),
                skills=tpl_data.get("skills", []),
                tools=tpl_data.get("tools", []),
                tags=tpl_data.get("tags", []),
                author="system",
            )
            self._templates[template.template_id] = template

        # 加载用户自定义模板
        if self._templates_file.exists():
            try:
                with open(self._templates_file, "r", encoding="utf-8") as f:
                    custom_data = json.load(f)
                for tpl_data in custom_data:
                    template = AgentTemplate(
                        template_id=tpl_data["template_id"],
                        name=tpl_data["name"],
                        display_name=tpl_data["display_name"],
                        description=tpl_data["description"],
                        category=tpl_data.get("category", "custom"),
                        icon=tpl_data.get("icon", ""),
                        version=tpl_data.get("version", "1.0.0"),
                        system_prompt=tpl_data.get("system_prompt", ""),
                        role=tpl_data.get("role", ""),
                        skills=tpl_data.get("skills", []),
                        tools=tpl_data.get("tools", []),
                        config=tpl_data.get("config", {}),
                        tags=tpl_data.get("tags", []),
                        author=tpl_data.get("author", "user"),
                        created_at=tpl_data.get("created_at", time.time()),
                        updated_at=tpl_data.get("updated_at", time.time()),
                        usage_count=tpl_data.get("usage_count", 0),
                        rating=tpl_data.get("rating", 0.0),
                    )
                    self._templates[template.template_id] = template
            except Exception as exc:
                logger.warning("Failed to load custom templates: %s", exc)

    def _save_custom_templates(self):
        """保存自定义模板。"""
        custom_templates = [
            t.__dict__ for t in self._templates.values()
            if not t.template_id.startswith("builtin_")
        ]
        with open(self._templates_file, "w", encoding="utf-8") as f:
            json.dump(custom_templates, f, indent=2, ensure_ascii=False)

    def list_templates(self, category: str = None, tag: str = None, search: str = None) -> List[AgentTemplate]:
        """列出模板（可按分类、标签、搜索过滤）。"""
        results = list(self._templates.values())

        if category:
            results = [t for t in results if t.category == category]

        if tag:
            results = [t for t in results if tag in t.tags]

        if search:
            search_lower = search.lower()
            results = [
                t for t in results
                if search_lower in t.display_name.lower()
                or search_lower in t.description.lower()
                or any(search_lower in tag.lower() for tag in t.tags)
            ]

        return sorted(results, key=lambda t: t.usage_count, reverse=True)

    def list_categories(self) -> List[TemplateCategory]:
        """列出所有分类。"""
        categories = []
        for cat_data in self.BUILTIN_CATEGORIES:
            cat = TemplateCategory(
                category_id=cat_data["category_id"],
                name=cat_data["name"],
                display_name=cat_data["display_name"],
                description=cat_data.get("description", ""),
                icon=cat_data.get("icon", ""),
            )
            cat.template_count = sum(1 for t in self._templates.values() if t.category == cat.category_id)
            categories.append(cat)
        return categories

    def get_template(self, template_id: str) -> Optional[AgentTemplate]:
        """获取模板详情。"""
        return self._templates.get(template_id)

    def create_template(self, name: str, display_name: str, description: str,
                        system_prompt: str = "", category: str = "custom",
                        icon: str = "", role: str = "", skills: List[str] = None,
                        tools: List[str] = None, config: Dict = None,
                        tags: List[str] = None, author: str = "user") -> AgentTemplate:
        """创建自定义模板。"""
        template_id = f"custom_{int(time.time())}"
        template = AgentTemplate(
            template_id=template_id,
            name=name,
            display_name=display_name,
            description=description,
            category=category,
            icon=icon,
            system_prompt=system_prompt,
            role=role,
            skills=skills or [],
            tools=tools or [],
            config=config or {},
            tags=tags or [],
            author=author,
        )
        self._templates[template_id] = template
        self._save_custom_templates()
        return template

    def update_template(self, template_id: str, **kwargs) -> Optional[AgentTemplate]:
        """更新模板。"""
        template = self._templates.get(template_id)
        if not template or template_id.startswith("builtin_"):
            return None

        for key, value in kwargs.items():
            if hasattr(template, key):
                setattr(template, key, value)

        template.updated_at = time.time()
        self._save_custom_templates()
        return template

    def delete_template(self, template_id: str) -> bool:
        """删除模板（仅自定义模板）。"""
        if template_id.startswith("builtin_"):
            return False
        if template_id in self._templates:
            del self._templates[template_id]
            self._save_custom_templates()
            return True
        return False

    def increment_usage(self, template_id: str):
        """增加使用计数。"""
        template = self._templates.get(template_id)
        if template:
            template.usage_count += 1
            if not template_id.startswith("builtin_"):
                self._save_custom_templates()

    def rate_template(self, template_id: str, rating: float) -> bool:
        """给模板评分。"""
        template = self._templates.get(template_id)
        if template:
            template.rating = (template.rating * template.usage_count + rating) / (template.usage_count + 1)
            if not template_id.startswith("builtin_"):
                self._save_custom_templates()
            return True
        return False


class AgentTemplatesPlugin(Plugin):
    """Agent模板市场插件。"""

    name = "agent_templates"

    def __init__(self):
        super().__init__()
        self._manager = None

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("agent_templates", {}) or {}
        data_dir = cfg.get("data_dir", "data/agent_templates")
        self._manager = TemplateManager(data_dir)
        logger.info("Agent templates plugin configured")

    def list_templates(self, category: str = None, tag: str = None, search: str = None) -> List[AgentTemplate]:
        """列出模板。"""
        if self._manager:
            return self._manager.list_templates(category, tag, search)
        return []

    def list_categories(self) -> List[TemplateCategory]:
        """列出分类。"""
        if self._manager:
            return self._manager.list_categories()
        return []

    def get_template(self, template_id: str) -> Optional[AgentTemplate]:
        """获取模板详情。"""
        if self._manager:
            return self._manager.get_template(template_id)
        return None

    def create_template(self, **kwargs) -> Optional[AgentTemplate]:
        """创建自定义模板。"""
        if self._manager:
            return self._manager.create_template(**kwargs)
        return None

    def apply_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """应用模板，返回配置字典。"""
        template = self.get_template(template_id)
        if not template:
            return None

        if self._manager:
            self._manager.increment_usage(template_id)

        return {
            "system_prompt": template.system_prompt,
            "role": template.role,
            "skills": template.skills,
            "tools": template.tools,
            "config": template.config,
        }

    def get_manager(self) -> Optional[TemplateManager]:
        """获取模板管理器。"""
        return self._manager