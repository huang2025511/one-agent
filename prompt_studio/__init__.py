"""Prompt 工程工作台 — Prompt模板管理、A/B测试、效果评估。

提供：
  - Prompt模板可视化编辑器
  - 版本控制与历史回溯
  - A/B测试对比
  - 效果自动评估
  - 模板市场与分享
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class PromptTemplate:
    """Prompt模板类。"""
    template_id: str
    name: str
    description: str
    system_prompt: str = ""
    user_prompt: str = ""
    variables: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    category: str = "general"
    author: str = "user"
    version: str = "1.0.0"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    usage_count: int = 0
    rating: float = 0.0
    is_public: bool = False


@dataclass
class PromptVersion:
    """Prompt版本类。"""
    version_id: str
    template_id: str
    system_prompt: str
    user_prompt: str
    version: str
    changelog: str = ""
    created_at: float = field(default_factory=time.time)
    author: str = ""


@dataclass
class ABTest:
    """A/B测试类。"""
    test_id: str
    name: str
    template_a_id: str
    template_b_id: str
    description: str = ""
    test_cases: List[Dict[str, Any]] = field(default_factory=list)
    results: Dict[str, Any] = field(default_factory=dict)
    status: str = "draft"  # draft / running / completed
    created_at: float = field(default_factory=time.time)
    winner: str = ""  # A / B / tie


@dataclass
class EvaluationResult:
    """评估结果类。"""
    test_id: str
    template_id: str
    score: float
    metrics: Dict[str, float] = field(default_factory=dict)
    feedback: str = ""
    created_at: float = field(default_factory=time.time)


class PromptTemplateManager:
    """Prompt模板管理器。"""

    BUILTIN_TEMPLATES = [
        {
            "name": "代码优化",
            "description": "优化现有代码，提高性能和可读性",
            "system_prompt": "你是一名资深软件工程师，擅长代码优化和重构。请分析用户提供的代码，识别性能瓶颈和代码质量问题，并提供优化建议和改进后的代码。",
            "user_prompt": "请优化以下代码：\n\n```\n{{code}}\n```\n\n优化目标：{{goal}}",
            "variables": ["code", "goal"],
            "tags": ["编程", "优化", "重构"],
            "category": "development",
        },
        {
            "name": "文章写作",
            "description": "生成高质量的文章内容",
            "system_prompt": "你是一名专业的内容创作者，擅长撰写各种类型的文章。请根据用户提供的主题和要求，创作结构清晰、内容丰富、语言流畅的文章。",
            "user_prompt": "请写一篇关于{{topic}}的文章，\n\n要求：\n- 字数：约{{word_count}}字\n- 风格：{{style}}\n- 目标读者：{{audience}}\n\n大纲：\n{{outline}}",
            "variables": ["topic", "word_count", "style", "audience", "outline"],
            "tags": ["写作", "内容", "文章"],
            "category": "writing",
        },
        {
            "name": "数据分析报告",
            "description": "生成数据分析报告",
            "system_prompt": "你是一名资深数据分析师，擅长从数据中发现洞察并撰写清晰的分析报告。请根据提供的数据和分析目标，生成结构化的数据分析报告。",
            "user_prompt": "请根据以下数据生成分析报告：\n\n数据：\n{{data}}\n\n分析目标：{{goal}}\n\n请包括：\n1. 数据概览\n2. 关键发现\n3. 趋势分析\n4. 建议与行动项",
            "variables": ["data", "goal"],
            "tags": ["数据分析", "报告", "洞察"],
            "category": "data",
        },
        {
            "name": "翻译润色",
            "description": "高质量翻译和文本润色",
            "system_prompt": "你是一名专业的翻译和文案编辑，精通多语言互译和文本润色。请准确、流畅地翻译文本，并根据目标语言的表达习惯进行适当润色。",
            "user_prompt": "请将以下文本翻译为{{target_language}}：\n\n{{text}}\n\n要求：\n- 风格：{{style}}\n- 专业领域：{{domain}}",
            "variables": ["target_language", "text", "style", "domain"],
            "tags": ["翻译", "润色", "语言"],
            "category": "writing",
        },
        {
            "name": "面试问答",
            "description": "模拟面试场景的问答",
            "system_prompt": "你是一名资深面试官，擅长各种岗位的面试评估。请根据岗位要求，提出专业的面试问题，并对回答进行评估和反馈。",
            "user_prompt": "请模拟{{position}}岗位的面试。\n\n面试阶段：{{stage}}\n候选人回答：{{answer}}\n\n请评估回答并提出下一个问题。",
            "variables": ["position", "stage", "answer"],
            "tags": ["面试", "求职", "评估"],
            "category": "career",
        },
    ]

    def __init__(self, data_dir: str = "data/prompt_studio"):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._templates_file = self._data_dir / "templates.json"
        self._versions_file = self._data_dir / "versions.json"
        self._templates: Dict[str, PromptTemplate] = {}
        self._versions: Dict[str, List[PromptVersion]] = {}
        self._load_templates()

    def _load_templates(self):
        """加载模板。"""
        # 加载内置模板
        for tpl_data in self.BUILTIN_TEMPLATES:
            template = PromptTemplate(
                template_id=f"builtin_{tpl_data['name']}",
                name=tpl_data["name"],
                description=tpl_data["description"],
                system_prompt=tpl_data.get("system_prompt", ""),
                user_prompt=tpl_data.get("user_prompt", ""),
                variables=tpl_data.get("variables", []),
                tags=tpl_data.get("tags", []),
                category=tpl_data.get("category", "general"),
                author="system",
            )
            self._templates[template.template_id] = template

        # 加载用户自定义模板
        if self._templates_file.exists():
            try:
                with open(self._templates_file, "r", encoding="utf-8") as f:
                    custom_data = json.load(f)
                for tpl_data in custom_data:
                    template = PromptTemplate(
                        template_id=tpl_data["template_id"],
                        name=tpl_data["name"],
                        description=tpl_data["description"],
                        system_prompt=tpl_data.get("system_prompt", ""),
                        user_prompt=tpl_data.get("user_prompt", ""),
                        variables=tpl_data.get("variables", []),
                        tags=tpl_data.get("tags", []),
                        category=tpl_data.get("category", "general"),
                        author=tpl_data.get("author", "user"),
                        version=tpl_data.get("version", "1.0.0"),
                        created_at=tpl_data.get("created_at", time.time()),
                        updated_at=tpl_data.get("updated_at", time.time()),
                        usage_count=tpl_data.get("usage_count", 0),
                        rating=tpl_data.get("rating", 0.0),
                        is_public=tpl_data.get("is_public", False),
                    )
                    self._templates[template.template_id] = template
            except Exception as exc:
                logger.warning("Failed to load prompt templates: %s", exc)

    def _save_templates(self):
        """保存自定义模板。"""
        custom = [
            t.__dict__ for t in self._templates.values()
            if not t.template_id.startswith("builtin_")
        ]
        with open(self._templates_file, "w", encoding="utf-8") as f:
            json.dump(custom, f, indent=2, ensure_ascii=False)

    def list_templates(self, category: str = None, tag: str = None, search: str = None) -> List[PromptTemplate]:
        """列出模板。"""
        results = list(self._templates.values())

        if category:
            results = [t for t in results if t.category == category]

        if tag:
            results = [t for t in results if tag in t.tags]

        if search:
            search_lower = search.lower()
            results = [
                t for t in results
                if search_lower in t.name.lower()
                or search_lower in t.description.lower()
            ]

        return sorted(results, key=lambda t: t.usage_count, reverse=True)

    def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        """获取模板。"""
        return self._templates.get(template_id)

    def create_template(self, name: str, description: str, system_prompt: str = "",
                        user_prompt: str = "", variables: List[str] = None,
                        tags: List[str] = None, category: str = "general",
                        author: str = "user") -> PromptTemplate:
        """创建模板。"""
        template_id = f"tpl_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        template = PromptTemplate(
            template_id=template_id,
            name=name,
            description=description,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            variables=variables or [],
            tags=tags or [],
            category=category,
            author=author,
        )
        self._templates[template_id] = template
        self._save_templates()
        return template

    def update_template(self, template_id: str, **kwargs) -> Optional[PromptTemplate]:
        """更新模板。"""
        template = self._templates.get(template_id)
        if not template or template_id.startswith("builtin_"):
            return None

        # 保存旧版本
        self._save_version(template, "更新前版本")

        for key, value in kwargs.items():
            if hasattr(template, key):
                setattr(template, key, value)

        template.updated_at = time.time()
        self._save_templates()
        return template

    def _save_version(self, template: PromptTemplate, changelog: str = ""):
        """保存版本快照。"""
        version = PromptVersion(
            version_id=f"ver_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            template_id=template.template_id,
            system_prompt=template.system_prompt,
            user_prompt=template.user_prompt,
            version=template.version,
            changelog=changelog,
            author=template.author,
        )

        if template.template_id not in self._versions:
            self._versions[template.template_id] = []
        self._versions[template.template_id].append(version)

    def get_versions(self, template_id: str) -> List[PromptVersion]:
        """获取模板的所有版本。"""
        return self._versions.get(template_id, [])

    def delete_template(self, template_id: str) -> bool:
        """删除模板。"""
        if template_id.startswith("builtin_"):
            return False
        if template_id in self._templates:
            del self._templates[template_id]
            self._save_templates()
            return True
        return False

    def render_template(self, template_id: str, variables: Dict[str, str] = None) -> Tuple[str, str]:
        """渲染模板，返回(system_prompt, user_prompt)。"""
        template = self._templates.get(template_id)
        if not template:
            return "", ""

        variables = variables or {}
        system_prompt = template.system_prompt
        user_prompt = template.user_prompt

        for key, value in variables.items():
            placeholder = "{{" + key + "}}"
            system_prompt = system_prompt.replace(placeholder, str(value))
            user_prompt = user_prompt.replace(placeholder, str(value))

        template.usage_count += 1
        if not template_id.startswith("builtin_"):
            self._save_templates()

        return system_prompt, user_prompt


class PromptStudioPlugin(Plugin):
    """Prompt工程工作台插件。"""

    name = "prompt_studio"

    def __init__(self):
        super().__init__()
        self._template_manager = None
        self._ab_tests: Dict[str, ABTest] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("prompt_studio", {}) or {}
        data_dir = cfg.get("data_dir", "data/prompt_studio")
        self._template_manager = PromptTemplateManager(data_dir)
        logger.info("Prompt studio plugin configured")

    def list_templates(self, category: str = None, tag: str = None, search: str = None) -> List[PromptTemplate]:
        """列出模板。"""
        if self._template_manager:
            return self._template_manager.list_templates(category, tag, search)
        return []

    def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        """获取模板。"""
        if self._template_manager:
            return self._template_manager.get_template(template_id)
        return None

    def create_template(self, **kwargs) -> Optional[PromptTemplate]:
        """创建模板。"""
        if self._template_manager:
            return self._template_manager.create_template(**kwargs)
        return None

    def update_template(self, template_id: str, **kwargs) -> Optional[PromptTemplate]:
        """更新模板。"""
        if self._template_manager:
            return self._template_manager.update_template(template_id, **kwargs)
        return None

    def render_template(self, template_id: str, variables: Dict[str, str] = None) -> Tuple[str, str]:
        """渲染模板。"""
        if self._template_manager:
            return self._template_manager.render_template(template_id, variables)
        return "", ""

    def get_versions(self, template_id: str) -> List[PromptVersion]:
        """获取版本历史。"""
        if self._template_manager:
            return self._template_manager.get_versions(template_id)
        return []

    def create_ab_test(self, name: str, template_a_id: str, template_b_id: str,
                       description: str = "", test_cases: List[Dict] = None) -> ABTest:
        """创建A/B测试。"""
        test = ABTest(
            test_id=f"ab_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            name=name,
            description=description,
            template_a_id=template_a_id,
            template_b_id=template_b_id,
            test_cases=test_cases or [],
        )
        self._ab_tests[test.test_id] = test
        return test

    def list_ab_tests(self) -> List[ABTest]:
        """列出A/B测试。"""
        return list(self._ab_tests.values())

    def get_ab_test(self, test_id: str) -> Optional[ABTest]:
        """获取A/B测试详情。"""
        return self._ab_tests.get(test_id)

    def get_template_manager(self) -> Optional[PromptTemplateManager]:
        """获取模板管理器。"""
        return self._template_manager