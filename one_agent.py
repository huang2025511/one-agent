"""Top-level One-Agent bootstrap.

Wires up every subsystem, then hands control to the CLI gateway or the web
UI.  Run with ``python one_agent.py`` (or ``python -m one_agent``).

Enhanced with:
  - Unified structured logging
  - Pydantic config validation
  - Fernet encryption for API keys
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.context import AgentContext  # noqa: E402
from core.plugin import PluginManager  # noqa: E402
from memory.session_store import SessionStore  # noqa: E402


# ============================================================
# Pydantic config models (validation at load time)
# ============================================================

class LLMApiKeys(BaseModel):
    model_config = {"extra": "allow"}  # Accept any provider's key (sensenova, zhipu, etc.)
    openrouter: Optional[str] = Field(default=None, description="OpenRouter API key")
    openai: Optional[str] = Field(default=None, description="OpenAI API key")
    anthropic: Optional[str] = Field(default=None, description="Anthropic API key")
    deepseek: Optional[str] = Field(default=None, description="DeepSeek API key")
    dashscope: Optional[str] = Field(default=None, description="Alibaba DashScope key")


class LLMConfig(BaseModel):
    primary_provider: str = "openrouter"
    primary_model: str = "anthropic/claude-3.5-sonnet"
    lightweight_model: str = "gpt-4o-mini"
    local_endpoint: str = "http://localhost:11434"
    local_model: str = "qwen2.5:7b"
    api_keys: LLMApiKeys = Field(default_factory=LLMApiKeys)
    default_temperature: float = Field(default=0.3, ge=0, le=2)
    default_max_tokens: int = Field(default=2048, ge=1)
    timeout: int = Field(default=60, ge=5)
    retries: int = Field(default=3, ge=1, le=10)
    cost_tracking: Dict[str, Any] = Field(default_factory=lambda: {"daily_budget": 1.0, "monthly_budget": 20.0, "db_path": "data/memory/costs.db"})


class RouterConfig(BaseModel):
    enabled: bool = True
    task_complexity_thresholds: Dict[str, float] = Field(
        default_factory=lambda: {"trivial": 0.2, "simple": 0.5, "complex": 0.8, "expert": 1.0}
    )
    context_compression: Dict[str, Any] = Field(
        default_factory=lambda: {"enabled": True, "min_tokens_before_compress": 2000, "compression_ratio": 0.4}
    )
    skill_lazy_loading: Dict[str, Any] = Field(
        default_factory=lambda: {"enabled": True, "max_skills_per_turn": 5, "ttl_seconds": 300}
    )
    self_evolution: Dict[str, Any] = Field(
        default_factory=lambda: {"enabled": True, "min_samples_before_adjust": 50}
    )


class MemoryShortTerm(BaseModel):
    max_turns: int = Field(default=20, ge=1)
    max_tokens: int = Field(default=8000, ge=100)


class MemoryLongTerm(BaseModel):
    enabled: bool = True
    storage: str = "sqlite-fts5"
    max_results: int = Field(default=5, ge=1)
    relevance_threshold: float = Field(default=0.6, ge=0, le=1)
    decay_enabled: bool = True


class MemoryProcedural(BaseModel):
    enabled: bool = True
    auto_create_skills: bool = True
    min_usage_before_skill: int = Field(default=3, ge=1)
    skill_storage: str = "markdown"


class MemoryConfig(BaseModel):
    short_term: MemoryShortTerm = Field(default_factory=MemoryShortTerm)
    long_term: MemoryLongTerm = Field(default_factory=MemoryLongTerm)
    procedural: MemoryProcedural = Field(default_factory=MemoryProcedural)


class AgentConfig(BaseModel):
    name: str = "One-Agent"
    description: str = "Token-efficient self-evolving microkernel AI agent"
    version: str = "2.0.0"
    data_dir: str = "./data"
    log_level: str = Field(default="INFO")
    timezone: str = Field(default="UTC")
    language: str = Field(default="en")  # 语言设置: en | zh

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(f"log_level must be one of {valid}, got {v}")
        return v.upper()


class FullConfig(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    skills: Dict[str, Any] = Field(default_factory=dict)
    gateways: Dict[str, Any] = Field(default_factory=dict)
    execution: Dict[str, Any] = Field(default_factory=dict)
    scheduler: Dict[str, Any] = Field(default_factory=dict)
    security: Dict[str, Any] = Field(default_factory=dict)
    rest: Dict[str, Any] = Field(default_factory=dict)
    monitoring: Dict[str, Any] = Field(default_factory=dict)
    multimodal: Dict[str, Any] = Field(default_factory=dict)
    marketplace: Dict[str, Any] = Field(default_factory=dict)
    llm_cache: Dict[str, Any] = Field(default_factory=dict)


# ============================================================
# Encryption helpers
# ============================================================

def _try_decrypt(value: str, cipher=None) -> str:
    if not value.startswith("enc:"):
        return value
    if cipher is None:
        return value  # key not available; leave as-is
    try:
        import base64
        token = value[4:]
        return cipher.decrypt(base64.b64decode(token)).decode()
    except Exception:
        return value


def _expand_env(obj):
    """Recursively expand ${VAR} references in loaded YAML."""
    if isinstance(obj, str):
        import re
        def repl(m):
            val = os.environ.get(m.group(1), "")
            return val if val else m.group(0)
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


# ============================================================
# Config loading with validation
# ============================================================

def load_config(path: str) -> FullConfig:
    """Load and validate config from YAML, expanding env vars and decrypting secrets.

    Returns a FullConfig Pydantic object (validated).  Call .model_dump() to get a
    plain dict for contexts that don't need Pydantic validation.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    expanded = _expand_env(raw)

    # Try Fernet decryption if key is set
    cipher = None
    enc_key = os.environ.get("ONE_AGENT_ENCRYPTION_KEY")
    if enc_key:
        try:
            from cryptography.fernet import Fernet
            cipher = Fernet(enc_key.encode())
            keys_section = expanded.get("llm", {}).get("api_keys", {})
            for provider, value in keys_section.items():
                if isinstance(value, str):
                    keys_section[provider] = _try_decrypt(value, cipher)
        except ImportError:
            pass

    return FullConfig(**expanded)


# ============================================================
# Structured logging setup
# ============================================================

def setup_logging(config) -> None:
    """Configure logging with file rotation and structured format."""
    log_dir = Path(config.agent.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s | %(levelname)7s | %(name)-30s | %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "one_agent.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(fmt, date_fmt))
    file_handler.setLevel(getattr(logging, config.agent.log_level))

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter(fmt, date_fmt))
    console_handler.setLevel(getattr(logging, config.agent.log_level))

    # Root logger
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.agent.log_level))
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Silence noisy third-party loggers
    for noisy in ["httpx", "httpcore", "urllib3", "asyncio"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ============================================================
# OneAgentApp
# ============================================================

class OneAgentApp:
    """Top-level assembly: builds plugin manager, coordinates plugins."""

    def __init__(self, config_path: str) -> None:
        from core.events import EventBus
        from core.coordinator import Coordinator
        from models import LLMProvider
        from router import SmartRouter
        from memory import MemoryPlugin
        from skills import SkillManager, Skill
        from memory.knowledge_graph import make_graph_search_handler
        from executors import ShellExecutor, DockerExecutor, BrowserExecutor, PythonExecutor
        from scheduler import SchedulerPlugin
        from multimodal import MultimodalPlugin
        from api import RESTAPIGateway
        from monitor import MonitoringPlugin
        from marketplace import MarketplacePlugin, Marketplace

        # Import gateways with graceful degradation — if a gateway's dependencies
        # are missing (e.g., cryptography for WeCom), log warning and skip it
        # rather than crashing the entire startup.
        from gateways import CLIGateway
        self.cli = CLIGateway()
        
        gateways_to_load = [
            ("telegram", "TelegramGateway"),
            ("wecom", "WeComGateway"),
            ("dingtalk", "DingTalkGateway"),
            ("feishu", "FeishuGateway"),
            ("discord", "DiscordGateway"),
            ("slack", "SlackGateway"),
            ("web", "WebGateway"),
        ]
        
        for attr_name, class_name in gateways_to_load:
            try:
                module = __import__("gateways", fromlist=[class_name])
                cls = getattr(module, class_name)
                setattr(self, attr_name, cls())
            except (ImportError, AttributeError) as e:
                logger.warning(f"Gateway {class_name} not available: {e}")
                setattr(self, attr_name, None)

        self.config = load_config(config_path)
        setup_logging(self.config)  # type: ignore[arg-type]

        self.bus = EventBus()

        self.llm = LLMProvider()
        self.router = SmartRouter()
        self.memory = MemoryPlugin()
        self.skills = SkillManager()
        self.exec_shell = ShellExecutor()
        self.exec_docker = DockerExecutor()
        self.exec_browser = BrowserExecutor()
        self.exec_python = PythonExecutor()
        self.coordinator = Coordinator()
        self.scheduler = SchedulerPlugin()
        self.multimodal = MultimodalPlugin()
        self.rest_api = RESTAPIGateway()
        self.monitor = MonitoringPlugin()
        self.marketplace = MarketplacePlugin()

        # Initialize alert manager
        from alerting import AlertManager
        self._alert_manager = AlertManager()

        # Initialize approval manager for human-in-the-loop
        from core.approval import ApprovalManager
        self._approval_manager = ApprovalManager()

        self._pm = PluginManager()
        for p in (
            self.llm, self.router, self.memory, self.skills,
            self.exec_shell, self.exec_docker, self.exec_browser,
            self.coordinator, self.scheduler,
            self.cli, self.telegram, self.wecom, self.dingtalk, self.feishu,
            self.discord, self.slack, self.web,
            self.multimodal, self.rest_api, self.monitor, self.marketplace,
        ):
            self._pm.register(p)

        self.ctx: Optional[AgentContext] = None

    async def start(self) -> None:
        # Initialize i18n based on config
        from i18n import set_language
        set_language(self.config.agent.language)
        
        self.ctx = AgentContext(
            config=self.config.model_dump(),
            bus=self.bus,
            data_dir=self.config.agent.data_dir,
        )
        # Create session store for persistence across restarts
        from pathlib import Path as _Path
        session_db_path = str(_Path(self.config.agent.data_dir) / "memory" / "sessions.db")
        session_store = SessionStore(session_db_path)
        self.ctx.session_store = session_store

        # Initialize self-improver for learning from failures
        from core.self_improve import SelfImprover
        improvements_db = str(_Path(self.config.agent.data_dir) / "memory" / "improvements.db")
        self.ctx.self_improver = SelfImprover(improvements_db)
        logger.info("self-improver initialized at %s", improvements_db)

        # Initialize skill marketplace
        from marketplace import Marketplace
        marketplace_dir = str(_Path(self.config.agent.data_dir) / "marketplace")
        self.ctx.marketplace = Marketplace(marketplace_dir)

        # Subscribe to turn_completed so every turn is auto-persisted
        async def _persist_turn(event):
            from core.events import Event as _Event
            turn = event.get("turn") if isinstance(event, _Event) else None
            if turn is None:
                return
            sid = getattr(turn, "session_id", "default")
            _store = self.ctx.session_store
            if _store is None:
                return
            # Save user message
            _store.add_message(sid, "user", turn.input_text,
                               meta={"source": turn.source, "turn_id": turn.turn_id},
                               tokens=turn.tokens_used)
            # Save assistant response
            if turn.result:
                _store.add_message(sid, "assistant", turn.result,
                                   meta={"model": turn.model or "", "turn_id": turn.turn_id},
                                   tokens=turn.tokens_used)
        self.bus.subscribe("turn_completed", _persist_turn)

        self.ctx._plugins = [
            self.llm, self.router, self.memory, self.skills,
            self.exec_shell, self.exec_docker, self.exec_browser,
            self.coordinator, self.scheduler,
            self.cli, self.telegram, self.wecom, self.dingtalk, self.feishu,
            self.discord, self.slack, self.web,
            self.multimodal, self.rest_api, self.monitor, self.marketplace,
        ]
        self.ctx._alert_manager = self._alert_manager

        # Attach approval manager to agent context
        self.ctx.approval_manager = self._approval_manager

        await self.bus.start()
        await self._pm.setup_all(self.ctx)
        # Register knowledge graph search skill (KG is created by MemoryPlugin during setup)
        if self.memory._kg is not None:
            from skills import Skill as _Skill
            from memory.knowledge_graph import make_graph_search_handler
            self.skills.register(_Skill(
                id="graph_search",
                title="知识图谱搜索",
                description="搜索实体关系：查询谁提到过什么、实体之间的关联信息。"
                            "使用 action 参数控制操作：search（搜索实体）、entity（查看实体详情）、neighbors（查看邻居图谱）。",
                schema={
                    "type": "function",
                    "function": {
                        "name": "graph_search",
                        "description": "搜索知识图谱中的实体和关系，支持实体搜索、详情查询和邻居图谱",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "description": "操作类型：search（搜索实体）、entity（查看实体详情和关系）、neighbors（查看邻居图谱）",
                                    "enum": ["search", "entity", "neighbors"],
                                },
                                "query": {
                                    "type": "string",
                                    "description": "搜索关键词（action=search 时使用）",
                                },
                                "name": {
                                    "type": "string",
                                    "description": "实体名称（action=entity 或 neighbors 时使用）",
                                },
                                "input": {
                                    "type": "string",
                                    "description": "搜索关键词或实体名称的别名",
                                },
                                "depth": {
                                    "type": "integer",
                                    "description": "邻居跳数（action=neighbors 时使用，默认 1）",
                                },
                                "limit": {
                                    "type": "integer",
                                    "description": "返回结果数量上限（默认 10）",
                                },
                            },
                            "required": [],
                        },
                    },
                },
                handler=make_graph_search_handler(self.memory._kg),
            ))
        self.router.bind_llm(self.llm)
        self.coordinator.bind(self.llm, self.skills)
        self.web.bind_callback(self.chat)
        self.rest_api.bind_callback(self.chat)
        # wire marketplace to skills plugin
        self.marketplace._skills_plugin = self.skills  # type: ignore[attr-defined]

        # Setup and start alert manager
        await self._alert_manager.setup(self.config.model_dump())
        await self._alert_manager.start()

        await self._pm.start_all()

        # Register daily self-improvement analysis task
        if self.scheduler is not None:
            def _analyze_improvements():
                """Periodic analysis of failure patterns and suggestion generation."""
                improver = getattr(self.ctx, "self_improver", None) if self.ctx else None
                if improver is None:
                    return
                patterns = improver.analyze_patterns()
                if patterns:
                    logger.info("self-improvement: found %d failure patterns", len(patterns))
                    for p in patterns:
                        suggestion = improver.generate_improvement(p.get("type", ""))
                        if suggestion:
                            improver.apply_improvement(p["type"], suggestion)
                            logger.info("self-improvement: applied '%s' → %s", p["type"], suggestion)
                else:
                    logger.debug("self-improvement: no patterns found")
            self.scheduler.add_cron("0 0 * * *", _analyze_improvements, "self_improve_analysis")
            logger.info("self-improvement daily analysis scheduled at midnight")

    async def chat(self, text: str, source: str = "cli", session_id: str = "default") -> str:
        from core.context import TurnContext
        turn = TurnContext(input_text=text, source=source, session_id=session_id)
        self.bus.publish({
            "type": "user_message",
            "payload": {"turn": turn, "session_id": session_id},
            "source": source,
        })
        import time as _time
        deadline = _time.monotonic() + 180
        while _time.monotonic() < deadline:
            if turn.result is not None or turn.error is not None:
                break
            await asyncio.sleep(0.1)
        return turn.result or "[timeout]"

    async def chat_with_thinking(self, text: str, source: str = "api", session_id: str = "default") -> dict:
        """Like chat(), but returns {"reply": ..., "thinking": ...} with thinking process.
        
        Uses its own TurnContext to avoid cross-request race conditions.
        """
        from core.context import TurnContext
        turn = TurnContext(input_text=text, source=source, session_id=session_id)
        self.bus.publish({
            "type": "user_message",
            "payload": {"turn": turn, "session_id": session_id},
            "source": source,
        })
        import time as _time
        deadline = _time.monotonic() + 180
        while _time.monotonic() < deadline:
            if turn.result is not None or turn.error is not None:
                break
            await asyncio.sleep(0.1)
        reply = turn.result or "[timeout]"
        thinking_text = turn.meta.get("thinking", "")
        return {"reply": reply, "session_id": session_id, "thinking": thinking_text}

    async def stop(self) -> None:
        await self._alert_manager.stop()
        await self._pm.stop_all()
        await self.bus.stop()


# ============================================================
# Entry point
# ============================================================

async def _interactive(app: OneAgentApp) -> None:
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║  One-Agent v2 — 自然语言即可操作，输入 '帮助'   ║")
    print("╚══════════════════════════════════════════════╝")

    # ---------- 自然语言意图匹配 ----------
    # 用户无需记住精准命令，用自然语言即可触发内置功能
    _INTENT_PATTERNS = {
        "exit": [
            r"退出|再见|拜拜|结束|关闭|退出程序|再见啦|bye|goodbye|see you",
        ],
        "help": [
            r"帮助|怎么用|使用说明|能做什么|有什么功能|help|命令列表|功能列表|怎么操作|使用方法",
        ],
        "skills": [
            r"技能|会什么|能做什么|有哪些能力|有什么技能|skill|能力列表|你会啥|你会什么",
        ],
        "status": [
            r"状态|运行状态|当前状态|系统状态|运行情况|status|还好吗|活着吗|运行多久",
        ],
        "metrics": [
            r"指标|统计|性能|调用量|token|用量|metrics|stats|统计数据|性能指标|使用量",
        ],
        "dlq": [
            r"死信|失败事件|未处理|错误队列|死信队列|dlq|dead.?letter|失败的消息",
        ],
        "bus": [
            r"事件|总线|event.?bus|事件类型|总线状态|bus",
        ],
        "clear": [
            r"清屏|清除屏幕|清理屏幕|clear|刷新屏幕",
        ],
        "settings": [
            r"设置|配置|修改设置|查看设置|更改|切换模型|改模型|改温度|开启|关闭|启用|禁用|把.*改|set.*to|change|configure",
        ],
        "models": [
            r"\bmodels?\b|模型列表|有哪些模型|看模型|看.*模型|可.*模型|所有模型|免费模型|列出模型|列出.*模型|model.*list",
        ],
        # 智能分层：把 provider 的全部模型按 free/paid、context、features 自动分配到 4 层
        "rebuild_tiers": [
            r"智能分层|自动分层|自动分配|重新分层|刷新分层|rebuild.?tiers|auto.?tier|"
            r"分层|分类|分档|auto.?classif|smart.?tier|分配.*模型|模型.*分配",
        ],
    }

    def _match_intent(text: str) -> Optional[str]:
        """从自然语言中匹配用户意图，返回命令名或 None。"""
        import re
        lower = text.lower().strip()
        exact_map = {
            "exit": "exit", "quit": "exit", "q": "exit",
            "help": "help", "?": "help",
            "skills": "skills", "status": "status",
            "metrics": "metrics", "stats": "metrics",
            "dlq": "dlq", "bus": "bus", "clear": "clear",
            "settings": "settings", "config": "settings",
        }
        if lower in exact_map:
            return exact_map[lower]
        for intent, patterns in _INTENT_PATTERNS.items():
            for pat in patterns:
                if re.search(pat, lower):
                    return intent
        return None

    session_id = "cli-session"
    while True:
        try:
            line = input("one-agent> ").strip()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print("\n(interrupted)")
            return
        if not line:
            continue
        intent = _match_intent(line)
        if intent == "exit":
            return
        if intent == "help":
            print("你可以用自然语言操作，也可以用精准命令：")
            print("  退出/再见/bye       → 退出程序")
            print("  帮助/怎么用/help    → 显示帮助")
            print("  技能/你会什么       → 列出技能")
            print("  状态/运行情况       → 系统状态")
            print("  指标/统计/metrics   → 性能指标")
            print("  死信/失败事件/dlq   → 死信队列")
            print("  事件/总线/bus       → 事件总线")
            print("  清屏/clear          → 清除屏幕")
            print("  设置/配置/改模型    → 修改设置")
            print("  其他任何文字        → 与 AI 对话")
            continue
        if intent == "skills":
            print("loaded:", ", ".join(app.skills.all_skill_ids()) or "(none)")
            continue
        if intent == "status":
            print("memory:", app.memory.stats() if app.memory else {})
            print("llm:", app.llm.stats() if app.llm else {})
            print("bus:", app.bus.metrics() if app.bus else {})
            continue
        if intent == "metrics":
            print("bus metrics:", app.bus.metrics())
            print("llm calls:", app.llm.stats() if app.llm else {})
            print("skills loaded:", app.skills.all_skill_ids())
            print("uptime:", app.ctx.uptime() if app.ctx else 0)
            continue
        if intent == "dlq":
            dlq = app.bus.get_dlq(10)
            print(f"dead-letter queue ({len(dlq)} items):")
            for e in dlq:
                print(f"  [{e.id}] {e.type} from {e.source}")
            continue
        if intent == "bus":
            print("event types registered:")
            print("  (use bus.metrics() for full stats)")
            continue
        if intent == "clear":
            print("\033c", end="")
            continue
        if intent == "settings":
            # 将设置请求路由到 settings 技能
            from skills import _process_settings_command
            result = _process_settings_command(line, app.config)
            print(result)
            continue
        if intent == "models":
            # 模型发现：拉取当前 provider 的模型列表并按需过滤
            llm = getattr(app, "llm", None)
            if llm is None:
                print("[llm provider not available]")
                continue
            # 解析意图过滤
            spec: Dict = {}
            low = line.lower()
            if any(k in low for k in ("free", "免费")):
                spec["free_only"] = True
            if any(k in low for k in ("paid", "收费")):
                spec["paid_only"] = True
            import re as _re
            m = _re.search(r"(\d+)\s*k\b", low)
            if m:
                spec["min_context"] = int(m.group(1)) * 1000
            models = await llm.list_models(**spec)
            if not models:
                print("[no models found — check API key / network]")
                continue
            print(f"\n{len(models)} models from '{llm._infer_primary_provider()}':")
            for i, m in enumerate(models, 1):
                ctx = f" ctx={m['context_length']:,}" if m.get("context_length") else ""
                free = " [FREE]" if m.get("is_free") else ""
                feats = f" ({','.join(m.get('features', [])[:3])})" if m.get("features") else ""
                print(f"  {i:2d}. {m['id']}{free}{ctx}{feats}")
            print()
            continue
        if intent == "rebuild_tiers":
            # 智能分层：拉取 provider 全部模型 → 自动分类到 4 层
            llm = getattr(app, "llm", None)
            if llm is None:
                print("[llm provider not available]")
                continue
            # 解析用户是否指定了 provider（默认当前 primary）
            import re as _re
            prov = None
            prov_match = _re.search(
                r"(?:provider|提供商|从|用|on)\s*[:：=]?\s*([a-z][a-z0-9_\-]*)",
                line, _re.IGNORECASE,
            )
            if prov_match:
                prov = prov_match.group(1)
            else:
                # 试中文提供商名（如果 resolver 可用）
                try:
                    from models.resolver import _extract_provider_hint
                    hint = _extract_provider_hint(line)
                    if hint and hint not in ("unknown", ""):
                        prov = hint
                except Exception:
                    pass
            try:
                result = await llm.rebuild_tiers(provider=prov)
            except Exception as exc:  # noqa: BLE001
                print(f"[rebuild_tiers error: {exc}]")
                continue
            if not result.get("ok"):
                print(f"[rebuild_tiers failed: {result.get('error')}]")
                continue
            print(f"\n✓ 已为 provider '{result['provider']}' 重新分层"
                  f"（共 {result['model_count']} 个模型）:")
            for tier in ("trivial", "simple", "complex", "expert"):
                models_in_tier = result["tiers"].get(tier, [])
                print(f"  [{tier:8s}] {len(models_in_tier)} 个")
                for mid in models_in_tier:
                    print(f"           · {mid}")
            diff = result.get("diff", {})
            added = sum(len(v.get("added", [])) for v in diff.values())
            removed = sum(len(v.get("removed", [])) for v in diff.values())
            if added or removed:
                print(f"\n  diff: +{added} added / -{removed} removed")
            print()
            continue
        try:
            reply = await asyncio.wait_for(
                app.chat(line, source="cli", session_id=session_id), timeout=180
            )
        except asyncio.TimeoutError:
            print("[timeout — try again]")
            continue
        print(reply)


async def main() -> None:
    cfg_path = os.environ.get("ONE_AGENT_CONFIG", str(ROOT / "config" / "default_config.yaml"))
    if not Path(cfg_path).exists():
        sys.exit(f"config not found: {cfg_path}")
    app = OneAgentApp(cfg_path)
    await app.start()

    # register graceful shutdown on SIGINT/SIGTERM
    # Use ``get_running_loop()`` — we're inside ``asyncio.run()`` so a
    # loop is guaranteed to exist (unlike ``get_event_loop()`` which is
    # deprecated in 3.10+ and raises in 3.12+ when no loop is set).
    loop = asyncio.get_running_loop()
    _shutdown_triggered = False
    _shutdown_task: Optional[asyncio.Task] = None  # prevent GC of shutdown task

    def _on_signal():
        nonlocal _shutdown_triggered, _shutdown_task
        if not _shutdown_triggered:
            _shutdown_triggered = True
            print("\n[shutting down...]")
            _shutdown_task = asyncio.create_task(app.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, OSError):
            pass  # Windows or lack of signal support

    try:
        await _interactive(app)
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())