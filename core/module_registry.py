"""按需加载模块注册表与延迟加载器。

设计目标：
  - 核心模块（llm/router/memory/skills/executors/coordinator/scheduler/cli）
    始终立即加载，保证 Agent 基本功能可用。
  - 可选模块（multimodal/monitor/marketplace/alerting/rag/automl/...）
    注册为"延迟模块"，在首次被访问或被显式调用时才 import + 实例化 + setup。
  - 通过配置文件的 ``modules`` 段控制每个可选模块的加载策略：
      - ``eager``: 启动时立即加载（与核心模块一起）
      - ``lazy`` : 按需加载（默认）
      - ``off``  : 不加载

用法::

    registry = ModuleRegistry()
    registry.register("multimodal", "multimodal", "MultimodalPlugin",
                      description="多模态能力增强")
    # 启动时加载核心模块 + eager 模块
    await registry.setup_eager(ctx)
    # 按需获取
    plugin = await registry.get("multimodal", ctx)
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from core.context import AgentContext
    from core.plugin import Plugin

logger = logging.getLogger(__name__)


class LoadPolicy(str, Enum):
    """模块加载策略。"""
    EAGER = "eager"   # 启动时立即加载
    LAZY = "lazy"     # 按需加载（默认）
    OFF = "off"       # 不加载


@dataclass
class ModuleEntry:
    """注册表中的单个模块条目。"""
    name: str                          # 模块唯一标识（与 Plugin.name 对应）
    import_path: str                   # Python 包路径，如 "multimodal"
    class_name: str                    # 插件类名，如 "MultimodalPlugin"
    description: str = ""              # 模块描述
    category: str = "optional"         # 分类：core / gateway / optional
    depends_on: List[str] = field(default_factory=list)  # 依赖的其他模块名
    load_policy: LoadPolicy = LoadPolicy.LAZY  # 加载策略
    _instance: Optional[Any] = None    # 已加载的实例（缓存）
    _loaded: bool = False              # 是否已加载
    _error: Optional[str] = None       # 加载失败时的错误信息


class ModuleRegistry:
    """模块注册表 — 管理所有可选模块的元数据与延迟加载。

    核心模块由 ``one_agent.py`` 直接实例化并注册到 PluginManager，
    可选模块通过本注册表管理，支持按需加载。
    """

    def __init__(self) -> None:
        self._entries: Dict[str, ModuleEntry] = {}
        self._ctx: Optional["AgentContext"] = None
        self._pm = None  # PluginManager 引用，用于注册已加载的插件

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        import_path: str,
        class_name: str,
        description: str = "",
        category: str = "optional",
        depends_on: Optional[List[str]] = None,
        load_policy: LoadPolicy = LoadPolicy.LAZY,
    ) -> ModuleEntry:
        """注册一个模块到注册表。"""
        entry = ModuleEntry(
            name=name,
            import_path=import_path,
            class_name=class_name,
            description=description,
            category=category,
            depends_on=depends_on or [],
            load_policy=load_policy,
        )
        self._entries[name] = entry
        logger.debug("registered module: %s (%s)", name, load_policy.value)
        return entry

    def set_policy(self, name: str, policy: LoadPolicy) -> None:
        """更新模块的加载策略。"""
        if name in self._entries:
            self._entries[name].load_policy = policy

    def set_policies_from_config(self, config: Dict[str, Any]) -> None:
        """从配置字典批量设置加载策略。

        配置格式::

            modules:
              multimodal: lazy      # 按需加载
              monitor: eager        # 启动时加载
              automl: off           # 不加载
        """
        modules_config = config.get("modules", {})
        for name, policy_str in modules_config.items():
            if name not in self._entries:
                logger.debug("config references unknown module: %s", name)
                continue
            try:
                policy = LoadPolicy(policy_str)
                self._entries[name].load_policy = policy
            except ValueError:
                logger.warning("invalid load policy '%s' for module %s", policy_str, name)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_modules(self) -> List[Dict[str, Any]]:
        """列出所有已注册模块的状态。"""
        return [
            {
                "name": e.name,
                "description": e.description,
                "category": e.category,
                "load_policy": e.load_policy.value,
                "loaded": e._loaded,
                "error": e._error,
            }
            for e in self._entries.values()
        ]

    def get_entry(self, name: str) -> Optional[ModuleEntry]:
        """获取模块条目。"""
        return self._entries.get(name)

    def is_loaded(self, name: str) -> bool:
        """检查模块是否已加载。"""
        entry = self._entries.get(name)
        return entry._loaded if entry else False

    def get_loaded_modules(self) -> List[str]:
        """获取所有已加载的模块名。"""
        return [e.name for e in self._entries.values() if e._loaded]

    def get_eager_modules(self) -> List[ModuleEntry]:
        """获取所有需要立即加载的模块。"""
        return [e for e in self._entries.values() if e.load_policy == LoadPolicy.EAGER]

    def get_lazy_modules(self) -> List[ModuleEntry]:
        """获取所有按需加载的模块。"""
        return [e for e in self._entries.values() if e.load_policy == LoadPolicy.LAZY]

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def bind(self, ctx: "AgentContext", pm: Any) -> None:
        """绑定 AgentContext 和 PluginManager，供后续加载使用。"""
        self._ctx = ctx
        self._pm = pm

    async def setup_eager(self, ctx: "AgentContext", pm: Any) -> List["Plugin"]:
        """加载所有 eager 策略的模块。

        Returns:
            成功加载的插件实例列表。
        """
        self._ctx = ctx
        self._pm = pm
        loaded: List[Any] = []

        for entry in self._entries.values():
            if entry.load_policy != LoadPolicy.EAGER:
                continue
            plugin = await self._load_entry(entry)
            if plugin is not None:
                loaded.append(plugin)

        if loaded:
            logger.info("eager-loaded %d modules: %s",
                        len(loaded), [p.name for p in loaded])
        return loaded

    async def get(self, name: str, ctx: "AgentContext" = None) -> Optional["Plugin"]:
        """按需获取模块实例。

        如果模块尚未加载，则执行延迟加载（import + 实例化 + setup）。
        如果模块策略为 off，则返回 None。
        """
        entry = self._entries.get(name)
        if entry is None:
            logger.warning("module not found: %s", name)
            return None

        if entry.load_policy == LoadPolicy.OFF:
            logger.debug("module %s is disabled (off)", name)
            return None

        # 已加载则直接返回缓存
        if entry._loaded and entry._instance is not None:
            return entry._instance

        # 使用传入的 ctx 或绑定的 ctx
        if ctx is not None:
            self._ctx = ctx

        return await self._load_entry(entry)

    async def _load_entry(self, entry: ModuleEntry) -> Optional["Plugin"]:
        """加载单个模块条目（import + 实例化 + setup + start）。"""
        if entry._loaded:
            return entry._instance

        # 先加载依赖
        for dep_name in entry.depends_on:
            dep_entry = self._entries.get(dep_name)
            if dep_entry and not dep_entry._loaded and dep_entry.load_policy != LoadPolicy.OFF:
                await self._load_entry(dep_entry)

        logger.info("loading module: %s (from %s.%s)",
                     entry.name, entry.import_path, entry.class_name)

        try:
            # 动态导入
            module = importlib.import_module(entry.import_path)
            cls = getattr(module, entry.class_name)

            # 实例化
            instance = cls()

            # setup
            if self._ctx is not None:
                await instance.setup(self._ctx)

            # start
            await instance.start()

            # 注册到 PluginManager（如果有）
            if self._pm is not None:
                self._pm.register(instance)

            entry._instance = instance
            entry._loaded = True
            entry._error = None

            logger.info("module %s loaded successfully", entry.name)
            return instance

        except ImportError as exc:
            entry._error = f"import failed: {exc}"
            logger.warning("module %s import failed: %s", entry.name, exc)
            return None
        except Exception as exc:
            entry._error = f"setup failed: {exc}"
            logger.error("module %s setup failed: %s", entry.name, exc, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 卸载
    # ------------------------------------------------------------------

    async def unload(self, name: str) -> bool:
        """卸载模块。"""
        entry = self._entries.get(name)
        if entry is None or not entry._loaded:
            return False

        try:
            if entry._instance is not None:
                await entry._instance.stop()
            entry._instance = None
            entry._loaded = False
            logger.info("module %s unloaded", name)
            return True
        except Exception as exc:
            logger.error("failed to unload %s: %s", name, exc)
            return False

    async def unload_all(self) -> None:
        """卸载所有已加载的模块。"""
        # 逆序卸载
        for entry in reversed(list(self._entries.values())):
            if entry._loaded:
                await self.unload(entry.name)

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """获取注册表整体状态。"""
        total = len(self._entries)
        loaded = sum(1 for e in self._entries.values() if e._loaded)
        eager = sum(1 for e in self._entries.values() if e.load_policy == LoadPolicy.EAGER)
        lazy = sum(1 for e in self._entries.values() if e.load_policy == LoadPolicy.LAZY)
        off = sum(1 for e in self._entries.values() if e.load_policy == LoadPolicy.OFF)

        return {
            "total_modules": total,
            "loaded": loaded,
            "eager": eager,
            "lazy": lazy,
            "off": off,
            "modules": self.list_modules(),
        }


# ======================================================================
# 默认模块注册表 — 注册所有可选模块
# ======================================================================

def create_default_registry() -> ModuleRegistry:
    """创建包含所有可选模块的默认注册表。

    核心模块（llm/router/memory/skills/executors/coordinator/scheduler/cli）
    不在此注册表中，它们由 one_agent.py 直接加载。
    """
    registry = ModuleRegistry()

    # --- 网关类 ---
    registry.register(
        name="telegram", import_path="gateways", class_name="TelegramGateway",
        description="Telegram 网关", category="gateway", load_policy=LoadPolicy.OFF,
    )
    registry.register(
        name="wecom", import_path="gateways", class_name="WeComGateway",
        description="企业微信网关", category="gateway", load_policy=LoadPolicy.OFF,
    )
    registry.register(
        name="dingtalk", import_path="gateways", class_name="DingTalkGateway",
        description="钉钉网关", category="gateway", load_policy=LoadPolicy.OFF,
    )
    registry.register(
        name="feishu", import_path="gateways", class_name="FeishuGateway",
        description="飞书网关", category="gateway", load_policy=LoadPolicy.OFF,
    )
    registry.register(
        name="discord", import_path="gateways", class_name="DiscordGateway",
        description="Discord 网关", category="gateway", load_policy=LoadPolicy.OFF,
    )
    registry.register(
        name="slack", import_path="gateways", class_name="SlackGateway",
        description="Slack 网关", category="gateway", load_policy=LoadPolicy.OFF,
    )
    registry.register(
        name="web", import_path="gateways", class_name="WebGateway",
        description="Web 网关", category="gateway", load_policy=LoadPolicy.OFF,
    )

    # --- 基础增强类 ---
    registry.register(
        name="multimodal", import_path="multimodal", class_name="MultimodalPlugin",
        description="多模态能力增强（图像生成/理解/语音）", category="enhancement",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="monitor", import_path="monitor", class_name="MonitoringPlugin",
        description="系统监控与指标收集", category="enhancement",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="marketplace", import_path="marketplace", class_name="MarketplacePlugin",
        description="技能市场", category="enhancement",
        depends_on=["skills"], load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="alerting", import_path="alerting", class_name="AlertManager",
        description="告警系统", category="enhancement",
        depends_on=["monitor"], load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="rest_api", import_path="api", class_name="RESTAPIGateway",
        description="REST API 服务器", category="enhancement",
        load_policy=LoadPolicy.LAZY,
    )

    # --- AI 能力扩展类 ---
    registry.register(
        name="rag", import_path="rag", class_name="RAGPlugin",
        description="RAG 知识库检索增强", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="automl", import_path="automl", class_name="AutoMLPlugin",
        description="AutoML 自动化机器学习", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="active_learning", import_path="active_learning", class_name="ActiveLearningPlugin",
        description="主动学习与自我进化", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="document_intelligence", import_path="document_intelligence",
        class_name="DocumentIntelligencePlugin",
        description="文档智能处理", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="multi_agent", import_path="multi_agent", class_name="MultiAgentPlugin",
        description="多智能体协作引擎", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="fine_tune", import_path="fine_tune", class_name="FineTuneManager",
        description="模型微调支持", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="recommender", import_path="recommender", class_name="RecommenderPlugin",
        description="推荐引擎", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="prompt_studio", import_path="prompt_studio", class_name="PromptStudioPlugin",
        description="Prompt 工程工作台", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="agent_templates", import_path="agent_templates",
        class_name="AgentTemplatesPlugin",
        description="Agent 模板市场", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="code_interpreter", import_path="code_interpreter",
        class_name="CodeInterpreterPlugin",
        description="代码解释器沙箱", category="ai_capability",
        load_policy=LoadPolicy.LAZY,
    )

    # --- 企业级功能类 ---
    registry.register(
        name="auth", import_path="auth", class_name="RBACManager",
        description="RBAC 访问控制", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="backup", import_path="backup", class_name="BackupManager",
        description="备份恢复系统", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="webhooks", import_path="webhooks", class_name="WebhookManager",
        description="Webhook 系统", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="tasks", import_path="tasks", class_name="TaskQueue",
        description="异步任务队列", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="ws", import_path="ws", class_name="WebSocketManager",
        description="WebSocket 实时消息推送", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="export", import_path="export", class_name="DataExporter",
        description="数据导出", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="data_lineage", import_path="data_lineage", class_name="DataLineagePlugin",
        description="数据血缘与可观测性", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="security", import_path="security", class_name="SecurityPlugin",
        description="安全性增强", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="performance", import_path="performance", class_name="PerformancePlugin",
        description="性能优化", category="enterprise",
        load_policy=LoadPolicy.LAZY,
    )

    # --- 前沿探索类 ---
    registry.register(
        name="smart_contract", import_path="smart_contract",
        class_name="SmartContractPlugin",
        description="智能合约生成与审计", category="frontier",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="spatial_3d", import_path="spatial_3d", class_name="Spatial3DPlugin",
        description="3D 空间交互", category="frontier",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="kg_visualizer", import_path="kg_visualizer",
        class_name="KnowledgeGraphVisualizerPlugin",
        description="知识图谱可视化", category="frontier",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="analytics", import_path="analytics", class_name="AnalyticsPlugin",
        description="对话分析与洞察", category="frontier",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="ux", import_path="ux", class_name="UXPlugin",
        description="用户体验增强", category="frontier",
        load_policy=LoadPolicy.LAZY,
    )
    registry.register(
        name="plugin_sdk", import_path="plugin_sdk", class_name="PluginSDKPlugin",
        description="插件开发 SDK", category="frontier",
        load_policy=LoadPolicy.LAZY,
    )

    return registry
