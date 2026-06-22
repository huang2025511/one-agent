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
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

# Local path setup — must precede local application imports
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

__version__ = "2.0.0"

from core.context import AgentContext  # noqa: E402
from core.plugin import PluginManager  # noqa: E402
from memory.session_store import SessionStore  # noqa: E402

logger = logging.getLogger(__name__)


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
    with open(path, encoding="utf-8") as f:
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
        from api import RESTAPIGateway
        from core.coordinator import Coordinator
        from core.events import EventBus
        from executors import BrowserExecutor, DockerExecutor, PythonExecutor, ShellExecutor

        # Import gateways with graceful degradation — if a gateway's dependencies
        # are missing (e.g., cryptography for WeCom), log warning and skip it
        # rather than crashing the entire startup.
        from gateways import CLIGateway
        from marketplace import MarketplacePlugin
        from memory import MemoryPlugin
        from models import LLMProvider
        from monitor import MonitoringPlugin
        from multimodal import MultimodalPlugin
        from router import SmartRouter
        from scheduler import SchedulerPlugin
        from skills import SkillManager
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

        # Initialize MCP client for external tool servers
        from skills.mcp_client import MCPClient
        self.mcp_client = MCPClient()

        # Initialize alert manager
        from alerting import AlertManager
        self._alert_manager = AlertManager()

        # Initialize approval manager for human-in-the-loop
        from core.approval import ApprovalManager
        self._approval_manager = ApprovalManager()

        self._pm = PluginManager()
        for p in (
            self.llm, self.router, self.memory, self.skills,
            self.exec_shell, self.exec_docker, self.exec_browser, self.exec_python,
            self.coordinator, self.scheduler,
            self.cli, self.telegram, self.wecom, self.dingtalk, self.feishu,
            self.discord, self.slack, self.web,
            self.multimodal, self.rest_api, self.monitor, self.marketplace,
            self._alert_manager,
        ):
            if p is not None:
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
            p for p in (
                self.llm, self.router, self.memory, self.skills,
                self.exec_shell, self.exec_docker, self.exec_browser, self.exec_python,
                self.coordinator, self.scheduler,
                self.cli, self.telegram, self.wecom, self.dingtalk, self.feishu,
                self.discord, self.slack, self.web,
                self.multimodal, self.rest_api, self.monitor, self.marketplace,
            ) if p is not None
        ]
        self.ctx._alert_manager = self._alert_manager

        # Attach approval manager to agent context
        self.ctx.approval_manager = self._approval_manager

        # Attach MCP client to agent context
        self.ctx.mcp_client = self.mcp_client

        # Attach Python executor to agent context (shared instance)
        self.ctx.python_executor = self.exec_python

        # Attach llm provider to ctx so skills can call set_api_key / rebuild_tiers
        self.ctx._llm = self.llm

        await self.bus.start()
        await self._pm.setup_all(self.ctx)
        # Register knowledge graph search skill (KG is created by MemoryPlugin during setup)
        if self.memory._kg is not None:
            from memory.knowledge_graph import make_graph_search_handler
            from skills import Skill as _Skill
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
        # Guard against optional gateways that may be None if their import failed
        if self.web is not None:
            self.web.bind_callback(self.chat)
        self.rest_api.bind_callback(self.chat)
        # wire marketplace to skills plugin
        self.marketplace._skills_plugin = self.skills  # type: ignore[attr-defined]

        # Wire the monitoring plugin's metrics collector into the alert
        # manager so _check_loop actually evaluates rules against live metrics.
        # AlertManager.setup is invoked by PluginManager.setup_all above
        # (depends_on=["monitoring"]); start/stop are handled by start_all/stop_all.
        if self.monitor is not None:
            self._alert_manager.set_metrics_getter(self.monitor.collect_metrics)

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
        return turn.result if turn.result is not None else "[timeout]"

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
        reply = turn.result if turn.result is not None else "[timeout]"
        thinking_text = turn.meta.get("thinking", "")
        return {"reply": reply, "session_id": session_id, "thinking": thinking_text}

    async def stop(self) -> None:
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
            r"^退出$|^再见$|^拜拜$|^结束$|^退出程序$|^bye$|^goodbye$|^quit$",
        ],
        "help": [
            r"^帮助$|^怎么用$|^使用说明$|^help$|^命令列表$|^功能列表$",
        ],
        "skills": [
            r"^技能$|^会什么$|^有什么技能$|^skill$|^能力列表$|^你会啥$|^你会什么$",
        ],
        "status": [
            r"^状态$|^运行状态$|^系统状态$|^status$|^运行情况$",
        ],
        "metrics": [
            r"^指标$|^统计$|^metrics$|^stats$|^性能指标$",
        ],
        "dlq": [
            r"^死信$|^死信队列$|^dlq$|^dead.?letter$",
        ],
        "bus": [
            r"^事件总线$|^bus$|^event.?bus$",
        ],
        "clear": [
            r"^清屏$|^clear$|^清除屏幕$",
        ],
        # settings 只匹配精确的 /set 命令格式，自然语言交给 LLM
        "settings": [
            r"^/set\s+|^设置\s+\S+\s*[=＝]|^配置\s+\S+\s*[=＝]|^/config\s+",
        ],
        "models": [
            r"^/models?$|^模型列表$|^列出模型$|^所有模型$|^免费模型$",
        ],
        # 智能分层：把 provider 的全部模型按 free/paid、context、features 自动分配到 4 层
        "rebuild_tiers": [
            r"^智能分层$|^自动分层$|^重新分层$|^刷新分层$|^rebuild.?tiers$",
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
        import re as _re_key

        # 检测待确认的 provider 集成命令（全部/免费/选择 N/取消）
        _pending = getattr(app.ctx, "_pending_provider", None)
        if _pending:
            _low = line.lower().strip()
            _handled = False
            if _low in ("取消", "cancel", "放弃", "exit", "退出"):
                app.ctx._pending_provider = None
                print("已取消集成。API key 已保存，可稍后重新发送服务商+key 来集成。")
                continue
            elif _low in ("全部", "all", "所有", "全部集成"):
                _handled = True
                _selected = _pending["models"]
            elif _low in ("免费", "free", "免费模型"):
                _handled = True
                _selected = _pending.get("free_models", [])
                if not _selected:
                    print("❌ 没有免费模型可选")
                    continue
            elif _re_key.match(r"(?:选择|select|pick)\s+([\d,\s]+)", _low):
                _handled = True
                _nums = [int(x) for x in _re_key.findall(r"\d+", _low)]
                _free = _pending.get("free_models", [])
                _selected = [_free[i - 1] for i in _nums if 0 < i <= len(_free)]
                if not _selected:
                    print(f"❌ 编号无效，请输入 1-{len(_free)} 之间的数字")
                    continue
            elif _re_key.match(r"^[\d,\s]+$", _low):
                # 直接输入数字 "1,3,5"
                _handled = True
                _nums = [int(x) for x in _re_key.findall(r"\d+", _low)]
                _free = _pending.get("free_models", [])
                _selected = [_free[i - 1] for i in _nums if 0 < i <= len(_free)]
                if not _selected:
                    print(f"❌ 编号无效，请输入 1-{len(_free)} 之间的数字")
                    continue

            if _handled:
                _prov = _pending["provider"]
                app.ctx._pending_provider = None
                print(f"🔄 正在将 {len(_selected)} 个模型集成到 {_prov}...")
                try:
                    _llm = getattr(app, "llm", None)
                    if _llm:
                        _result = await _llm.rebuild_tiers(provider=_prov, persist=True)
                        if _result.get("ok"):
                            _tiers = _result.get("tiers", {})
                            print(f"✅ 集成成功！共 {_result.get('model_count', 0)} 个模型已分配到 4 层：")
                            for _tier_name in ("trivial", "simple", "complex", "expert"):
                                _models = _tiers.get(_tier_name, [])
                                print(f"  [{_tier_name:8s}] {len(_models)} 个: {', '.join(_models[:3])}{'...' if len(_models) > 3 else ''}")
                            print(f"\n💡 如需切换到此服务商，可以对我说：'使用{_prov}' 或 '/set provider {_prov}'")
                        else:
                            print(f"⚠️ 集成失败: {_result.get('error', 'unknown')}")
                    else:
                        print("❌ LLM provider 不可用")
                except Exception as exc:
                    print(f"❌ 集成错误: {exc}")
                continue
            # 不是确认命令，清除待确认状态，继续正常处理
            app.ctx._pending_provider = None

        # 检测"服务商 + API key"模式，直接调用 add_provider 技能
        # 不依赖 LLM 工具调用（某些模型不支持 tools）
        # 支持多行输入：如果当前行只有 key，检查上一行是否是服务商名
        _key_only = _re_key.match(r"^\s*key\s*[=：:]\s*(\S+)\s*$", line, _re_key.IGNORECASE)
        if _key_only:
            _last_provider = getattr(app, "_last_provider_hint", None)
            if _last_provider:
                line = f"{_last_provider} {_key_only.group(1)}"
                app._last_provider_hint = None
        if _re_key.search(r"(?:key[:：]?\s*)?(nvapi-\S+|sk-\S+|ak-\S+)", line):
            try:
                from skills import SkillManager
                _sm = getattr(app, "_skills_mgr", None)
                if _sm is None:
                    # 临时创建 SkillManager 来执行 add_provider
                    _sm = SkillManager()
                    await _sm.setup(app.ctx)
                    app._skills_mgr = _sm
                _result = await _sm.dispatch("add_provider", {"input": line})
                if _result and str(_result) != "__SKIP__":
                    print(_result)
                    continue
            except Exception as exc:
                print(f"[add_provider error: {exc}]")
                continue
        # 检测"搜索 服务商名"命令：网上搜索未知服务商的 API 地址
        _search_m = _re_key.match(r"^(?:搜索|search|查找|find)\s+(.+)", line, _re_key.IGNORECASE)
        if _search_m:
            _provider_query = _search_m.group(1).strip()
            print(f"🔍 正在网上搜索「{_provider_query}」的 API 地址...")
            try:
                from models.resolver import resolve
                _resolved = await resolve(_provider_query, probe=True, timeout=8.0)
                if _resolved.found:
                    print(f"✅ 找到「{_provider_query}」的 API 地址：{_resolved.base_url}")
                    print(f"   （来源：{_resolved.via}）")
                    print(f"   现在你可以发送：{_provider_query} {_resolved.base_url} key=你的API密钥")
                else:
                    print(f"❌ 未能自动找到「{_provider_query}」的 API 地址。")
                    print("   请直接提供 base URL，例如：")
                    print(f"   {_provider_query} https://api.example.com/v1 key=你的API密钥")
            except Exception as exc:
                print(f"❌ 搜索失败: {exc}")
                print("   请直接提供 base URL，例如：")
                print(f"   {_provider_query} https://api.example.com/v1 key=你的API密钥")
            continue
        # 保存服务商名作为 hint（用于多行输入：用户先发服务商名，下一行发 key）
        try:
            from models.resolver import _PROVIDER_ALIASES
            _hint = _re_key.sub(r"[，,。.：:：]+$", "", line.strip())
            if _hint in _PROVIDER_ALIASES or _hint.lower() in _PROVIDER_ALIASES:
                app._last_provider_hint = _hint
        except Exception:
            pass
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
            # None 或 "__SKIP__" 表示不是设置命令，跳过继续正常对话
            if result is None or result == "__SKIP__":
                pass  # 继续到下面的 LLM 对话
            else:
                print(result)
            # 只有明确返回了内容才结束，继续时不要 continue
            if result is not None and result != "__SKIP__":
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
        # 检测退出标志（quit_handler 设置）
        _quit_ev = getattr(app.ctx, "_quit_event", None)
        if _quit_ev is not None and _quit_ev.is_set():
            return


async def main() -> None:
    # ── Load .env file if it exists ──
    # This makes environment variables from .env available to the config
    env_file = ROOT / ".env"
    if env_file.exists():
        try:
            import dotenv
            dotenv.load_dotenv(env_file)
        except ImportError:
            # Fallback: manually parse .env if python-dotenv is not installed
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        val = val.strip()
                        # Strip surrounding quotes (single or double)
                        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                            val = val[1:-1]
                        os.environ.setdefault(key.strip(), val)

    # ── auto-detect missing API key and run setup wizard ──
    # This runs BEFORE the agent starts, so the user sees a clean
    # guided setup instead of confusing error messages.
    try:
        from core.setup_wizard import setup_if_needed
        _setup_ran = setup_if_needed()
    except Exception as exc:  # noqa: BLE001
        _setup_ran = False
        print(f"[setup wizard unavailable: {exc}]")

    cfg_path = os.environ.get("ONE_AGENT_CONFIG", str(ROOT / "config" / "default_config.yaml"))
    if not Path(cfg_path).exists():
        sys.exit(f"config not found: {cfg_path}")
    app = OneAgentApp(cfg_path)
    await app.start()

    # If the setup wizard ran, show a brief notice that keys were saved
    if _setup_ran:
        print("[已保存配置到 .env 文件，下次启动无需重新设置]")

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
            print("\n\n========================================")
            print("  👋 再见！One-Agent 已关闭")
            print("========================================\n")
            _shutdown_task = asyncio.create_task(app.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, OSError):
            pass  # Windows or lack of signal support

    try:
        await _interactive(app)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Clean exit on Ctrl+C or signal
        print("\n\n========================================")
        print("  👋 再见！One-Agent 已关闭")
        print("========================================\n")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())


def cli():
    """Console-script entry point for ``one-agent`` command."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
