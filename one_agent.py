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
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

# Local path setup — must precede local application imports
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

__version__ = "2.2.0"

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
    # 允许保留未声明的字段：LLMProvider.setup 会读取很多 llm 段下的
    # 配置项（auto_classify_on_setup / cache_enabled / cache_ttl_seconds /
    # cache_max_size / base_urls / fallback_chain 等），如果这里禁止 extra，
    # Pydantic 会静默丢弃它们，导致 auto_classify_on_setup=False 不生效、
    # 后台 auto-classify 仍然跑、阻塞 setup 等隐蔽 bug。
    model_config = {"extra": "allow"}
    # 修复：默认 provider/model 从 anthropic 改为 sensenova（商汤）。
    # 之前默认 anthropic，但 .env.example / setup_wizard / dev_config 都
    # 推荐 sensenova 为首选低成本 provider。当 default_config.yaml 缺失
    # primary_model 时，Pydantic 用此默认值 → _default_model = anthropic
    # → 用户未配 anthropic key → model_for_tier 回退到无 key 的 anthropic
    # → chat_completion 返回 no_api_key（0 tokens）。改为 sensenova 后，
    # 即使用户只在 .env 配了 SENSENOVA_API_KEY 也能直接跑通。
    primary_provider: str = "sensenova"
    primary_model: str = "sensenova/sensenova-6.7-flash-lite"
    lightweight_model: str = "sensenova/sensenova-6.7-flash-lite"
    local_endpoint: str = "http://localhost:11434"
    local_model: str = "qwen2.5:7b"
    api_keys: LLMApiKeys = Field(default_factory=LLMApiKeys)
    default_temperature: float = Field(default=0.3, ge=0, le=2)
    default_max_tokens: int = Field(default=2048, ge=1)
    timeout: int = Field(default=60, ge=5)
    retries: int = Field(default=3, ge=1, le=10)
    # v2.1.0：db_path 已移除 — 成本记录进统一库 one_agent.db（cost_log 表）
    cost_tracking: Dict[str, Any] = Field(default_factory=lambda: {"daily_budget": 1.0, "monthly_budget": 20.0})


class RouterConfig(BaseModel):
    # 修复：允许 extra 字段，与 LLMConfig 保持一致。
    # 之前缺少 extra="allow"，导致客户端 PUT router.tier_models 等扩展字段时
    # Pydantic 校验失败返回 400，所有设置保存都显示"保存失败"。
    model_config = {"extra": "allow"}
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
    version: str = "2.2.0"
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
    except Exception as exc:
        logger.warning("decrypt failed for field, returning raw: %s", type(exc).__name__)
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

def _deep_merge_dict(base: dict, override: dict) -> dict:
    """递归深度合并两个配置字典：dict 类型键递归合并，其他类型直接覆盖。

    用于 ONE_AGENT_ENV 触发时把 default_config.yaml 作为基础、再用 env 配置
    （dev/staging/prod）覆盖：env 只声明需要差异化的段，其余从 default 继承。
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge_dict(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: str) -> FullConfig:
    """Load and validate config from YAML, expanding env vars and decrypting secrets.

    Returns a FullConfig Pydantic object (validated).  Call .model_dump() to get a
    plain dict for contexts that don't need Pydantic validation.

    修复 #2：ONE_AGENT_ENV 触发的 env 配置（dev/staging/prod）只覆盖部分段，
    之前不与 default_config.yaml 合并，导致 rest/memory/skills 等段缺失，
    回退到 Pydantic 默认值（如 rest.rate_limit_per_minute 从 10000 降到 60、
    memory.long_term.storage 丢失等）。现在先加载 default 作为基础，再用 env
    配置深度覆盖。注意：ONE_AGENT_CONFIG 显式指定的路径不合并（用户明确要完全
    用那个文件）；只有 ONE_AGENT_ENV 触发、且 path 正是 env 解析出的配置时才合并。
    """
    default_path = str(ROOT / "config" / "default_config.yaml")
    explicit = os.environ.get("ONE_AGENT_CONFIG")
    env_name = os.environ.get("ONE_AGENT_ENV", "").strip().lower()

    # 仅当 ONE_AGENT_ENV 触发、ONE_AGENT_CONFIG 未显式指定、且 path 正是
    # config/{env}_config.yaml（非 default 本身）时，才与 default 深度合并。
    should_merge = False
    if env_name and not explicit:
        env_path = str(ROOT / "config" / f"{env_name}_config.yaml")
        try:
            same_path = Path(path).resolve() == Path(env_path).resolve()
        except Exception:
            same_path = path == env_path
        if same_path and env_path != default_path:
            should_merge = True

    if should_merge and Path(default_path).exists():
        with open(default_path, encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        with open(path, encoding="utf-8") as f:
            override = yaml.safe_load(f) or {}
        raw = _deep_merge_dict(base, override)
    else:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    # 配置库覆盖层（v2.1.0 数据统一）：{data_dir}/one_agent.db 优先于
    # YAML。YAML 仅在首次启动时被 seed_from_config 导入 DB，此后运行时
    # 修改只落 DB —— 迁移环境只需复制 one_agent.db 一个文件。
    # path 位于项目 config/ 之外的 yaml（单测自建文件）不叠加：完全接管。
    try:
        from core.config_store import (
            config_db_path,
            get_config_store,
            overlay_enabled,
            resolve_data_dir,
        )
        if overlay_enabled(path):
            _dd = resolve_data_dir(raw)
            # 旧版分散库（config.db、memory/*.db）一次性自动迁入统一库。
            # 必须先于 overlay：旧 config.db 的 settings 要先进 one_agent.db
            # 才能参与覆盖。
            from core.hub import migrate_legacy
            try:
                _report = migrate_legacy(_dd)
                if _report:
                    logger.info("legacy stores migrated into unified db: %s", _report)
            except Exception as exc:
                logger.warning("legacy migration failed (continuing): %s", exc)
            _db = config_db_path(_dd)
            if _db.exists():
                snap = get_config_store(str(_db)).snapshot()
                if snap:
                    raw = _deep_merge_dict(raw, snap)
    except Exception as exc:
        logger.warning("config db overlay failed, falling back to yaml only: %s", exc)

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
    # v2.1.0：日志目录同样走统一解析（ONE_AGENT_DATA_DIR 优先）
    from core.hub import resolve_data_dir
    log_dir = Path(resolve_data_dir(
        {"agent": {"data_dir": config.agent.data_dir}})) / "logs"
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

    # Install sensitive info filter on root logger — all child loggers inherit
    from core.log_sanitizer import install_sensitive_info_filter
    install_sensitive_info_filter(root)

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

        # v2.2.0：统一库每日自动备份（订阅 scheduler 的 db_backup cron）
        from core.db_maintenance import DBMaintenancePlugin
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
        self.db_maintenance = DBMaintenancePlugin()
        self.cli = CLIGateway()

        gateways_to_load = [
            ("telegram", "TelegramGateway"),
            ("wecom", "WeComGateway"),
            ("dingtalk", "DingTalkGateway"),
            ("feishu", "FeishuGateway"),
            ("discord", "DiscordGateway"),
            ("slack", "SlackGateway"),
            ("web", "WebGateway"),
            ("wechat_personal", "WeChatPersonalGateway"),
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

        # 首次启动：把 YAML 配置种子导入统一库的 settings 表（此后 YAML
        # 只读，运行时修改全部落 {data_dir}/one_agent.db，
        # 迁移环境 = 复制 one_agent.db 一个文件）。
        # config_path 位于项目 config/ 之外（单测自建 yaml）时不种子。
        try:
            from core.config_store import overlay_enabled, seed_from_config
            if overlay_enabled(str(config_path)) and seed_from_config(self.config):
                logger.info("config db seeded from %s (yaml is now read-only seed)",
                            config_path)
        except Exception as exc:
            logger.warning("config db seed skipped: %s", exc)

        self.bus = EventBus()

        # v2.1.0 修复：统一数据目录在此一次解析（ONE_AGENT_DATA_DIR >
        # agent.data_dir > ./data）。此前各存储点直接用
        # config.agent.data_dir，会绕过环境变量 —— 与 hub 无参调用
        # （走环境变量）指向两个不同的库文件，造成数据分裂。
        from core.hub import resolve_data_dir
        self.data_dir = resolve_data_dir(
            {"agent": {"data_dir": self.config.agent.data_dir}})

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
        # v2.1.0：审批记录持久化到统一库 one_agent.db（approvals 表），
        # 重启后待办不丢（DB 故障时自动降级纯内存，不影响审批功能）
        from core.approval import ApprovalManager
        from core.hub import database_path
        self._approval_manager = ApprovalManager(
            db_path=database_path(self.data_dir),
        )

        self._pm = PluginManager()
        for p in (
            self.llm, self.router, self.memory, self.skills,
            self.exec_shell, self.exec_docker, self.exec_browser, self.exec_python,
            self.coordinator, self.scheduler,
            self.cli, self.telegram, self.wecom, self.dingtalk, self.feishu,
            self.discord, self.slack, self.web, self.wechat_personal,
            self.multimodal, self.rest_api, self.monitor, self.marketplace,
            self._alert_manager, self.db_maintenance,
        ):
            if p is not None:
                self._pm.register(p)

        self.ctx: Optional[AgentContext] = None
        self._recent_restart: float = 0  # 重启时间戳，0 表示非重启启动

    async def start(self) -> None:
        # Initialize i18n based on config
        from i18n import set_language
        set_language(self.config.agent.language)

        # 修复：预热所有关键单例, 消除"首次调用"竞态窗口。
        # 之前 30+ 处 get_xxx() 无锁, 在 sync 多线程入口 (如 webhook 回调)
        # 并发首次调用时可能重复实例化, 导致持有 SQLite 连接/Lock 的单例
        # 状态分裂 (两个 AlertManager 各自告警 / 两个 CircuitManager 独立计数)。
        # 在单线程的 start() 阶段同步预热, 后续 get_xxx() 只读返回, 无竞态。
        try:
            from core.alerting import get_alert_manager
            from core.chart_gen import get_chart_generator
            from core.circuit_breaker import get_circuit_manager
            from core.conv_branch import get_branch_manager
            from core.failure_recovery import get_failure_recovery
            from core.rate_limiter import get_rate_limiter
            from core.streaming import StreamingBuffer
            from core.tool_cache import get_tool_cache
            from core.webhook_trigger import get_webhook_trigger

            mgr = get_alert_manager()
            mgr.setup_default_rules()
            get_circuit_manager()
            get_rate_limiter()
            get_failure_recovery()
            get_tool_cache()
            get_webhook_trigger()
            get_chart_generator()
            get_branch_manager()
            # 预热 StreamingBuffer：触发 core/streaming.py 模块加载与
            # 默认参数构造，避免首个流式请求时才初始化（与其它单例一致）。
            StreamingBuffer()
            # 预热分布式追踪单例：coordinator 关键路径已接入 span，
            # 启动阶段同步创建，避免首个请求时才初始化。
            from monitor.tracing import get_tracer
            get_tracer()
            logger.debug("singletons pre-warmed at startup")
        except Exception as exc:
            logger.warning("singleton pre-warm failed (non-fatal): %s", exc)

        # 检查重启标记 — 如果刚重启过，记录时间供系统提示词使用
        # v2.1.0：标记存统一库 kv（skills/restart_handler 写入 kv key
        # "restart_marker"），不再落 restart_marker.json 文件
        import time as _time
        try:
            from core.hub import get_hub
            marker_data = get_hub().kv_get("restart_marker")
            if isinstance(marker_data, dict):
                restart_ts = marker_data.get("timestamp", 0)
                if _time.time() - restart_ts < 120:  # 2 分钟内的重启
                    self._recent_restart = restart_ts
                    logger.info("detected recent restart at %s", restart_ts)
                get_hub().kv_delete("restart_marker")
            elif marker_data is not None:
                logger.warning("restart_marker 内容不是 dict：%s，已忽略",
                               type(marker_data).__name__)
        except Exception as exc:
            logger.warning("读取重启标记失败: %s", exc, exc_info=True)

        self.ctx = AgentContext(
            config=self.config.model_dump(),
            bus=self.bus,
            data_dir=self.data_dir,
        )
        # 传递重启标记给上下文
        self.ctx.recent_restart = self._recent_restart
        # Create session store for persistence across restarts
        # v2.1.0：会话/聊天记录进统一库 one_agent.db（sessions/messages 表）
        from core.hub import database_path
        session_store = SessionStore(database_path(self.data_dir))
        self.ctx.session_store = session_store

        # Initialize self-improver for learning from failures
        from core.self_improve import SelfImprover
        self.ctx.self_improver = SelfImprover(database_path(self.data_dir))
        logger.info("self-improver initialized at %s", database_path(self.data_dir))

        # Initialize skill marketplace
        from marketplace import Marketplace
        marketplace_dir = str(Path(self.data_dir) / "marketplace")
        self.ctx.marketplace = Marketplace(marketplace_dir)
        # 保留 MarketplacePlugin 引用（用于公开社区市场拉取）
        self.ctx.marketplace_plugin = self.marketplace

        # Subscribe to turn_completed so every turn is auto-persisted
        _seen_sessions = set()
        async def _persist_turn(event):
            from core.events import Event as _Event
            turn = event.get("turn") if isinstance(event, _Event) else None
            if turn is None:
                return
            sid = getattr(turn, "session_id", "default")
            _store = self.ctx.session_store
            if _store is None:
                return
            # Track session creation
            is_new_session = sid not in _seen_sessions
            if is_new_session:
                _seen_sessions.add(sid)
            # Save user message
            _store.add_message(sid, "user", turn.input_text,
                               meta={"source": turn.source, "turn_id": turn.turn_id},
                               tokens=turn.tokens_used)
            # Save assistant response — 失败时也要保存占位回复，
            # 否则只存 user 不存 assistant 会导致后续历史配对错位
            # （router 的 _history_tail 按 user→assistant 顺序配对，
            #  缺失的 assistant 会让下一轮的 reply 错配到本轮的 input）
            if turn.result:
                # 问题3 修复：持久化时保存 thinking（思考过程），否则
                # 切换会话/重开 App 后历史消息的思考过程全部丢失。
                # turn.meta["thinking"] 由 coordinator 在 think 阶段设置。
                _thinking = ""
                try:
                    _thinking = (turn.meta.get("thinking") or "") if turn.meta else ""
                except Exception:
                    pass
                _store.add_message(sid, "assistant", turn.result,
                                   meta={"model": turn.model or "", "turn_id": turn.turn_id,
                                         "thinking": _thinking},
                                   tokens=turn.tokens_used)
            elif turn.error:
                _reply = f"[处理失败] {turn.error}" if turn.error else "[处理失败]"
                _store.add_message(sid, "assistant", _reply,
                                   meta={"model": turn.model or "", "turn_id": turn.turn_id,
                                         "error": True},
                                   tokens=turn.tokens_used)
            # Publish session events
            try:
                if is_new_session:
                    self.bus.publish({"type": "session_created", "session_id": sid})
                self.bus.publish({"type": "session_updated", "session_id": sid})
            except Exception:
                logger.debug("session event publish failed", exc_info=True)
        self.bus.subscribe("turn_completed", _persist_turn)

        self.ctx._plugins = [
            p for p in (
                self.llm, self.router, self.memory, self.skills,
                self.exec_shell, self.exec_docker, self.exec_browser, self.exec_python,
                self.coordinator, self.scheduler,
                self.cli, self.telegram, self.wecom, self.dingtalk, self.feishu,
                self.discord, self.slack, self.web, self.wechat_personal,
                self.multimodal, self.rest_api, self.monitor, self.marketplace,
                # 之前漏掉 alert_manager，导致 ctx.get_plugin("alerting") 返回 None，
                # 与 _pm 注册形成不对称。approval_manager 不是 Plugin 子类
                # （无 name 属性），仍通过 ctx.approval_manager 访问。
                self._alert_manager,
            ) if p is not None
        ]
        self.ctx._alert_manager = self._alert_manager

        # Attach approval manager to agent context
        self.ctx.approval_manager = self._approval_manager

        # Attach MCP client to agent context
        self.ctx.mcp_client = self.mcp_client

        # Attach Python executor to agent context (shared instance)
        self.ctx.python_executor = self.exec_python

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

        # Publish startup event
        try:
            self.bus.publish({"type": "startup", "timestamp": time.time()})
        except Exception:
            logger.debug("startup event publish failed", exc_info=True)

        # 接入 webhook_trigger 到事件总线 — 关键业务事件自动触发已注册的 webhook
        # 修复审计发现的"WebhookTrigger 已写好但未启用"问题：
        # 之前 get_webhook_trigger() 单例在 start() 中预热、close() 在 stop()
        # 中调用，但 register()/trigger() 从未被业务代码调用，事件→HTTP webhook
        # 链路完全断开。此处订阅关键事件并转发到 trigger_all()。
        #
        # 注意：coordinator 只 publish "turn_completed"（成功/失败都走它，失败时
        # turn.error 非空）和 "user_message"；不存在 "turn_error" 事件
        # （EventBus._ALLOWED_EVENT_TYPES 里有 turn_failed 但 coordinator
        # 从未发布）。approval_needed 由 executors 在请求人工审批时发布。
        try:
            from core.webhook_trigger import get_webhook_trigger
            webhook_trigger = get_webhook_trigger()

            async def _on_turn_completed(evt):
                """turn_completed 事件触发 webhook；turn.error 时额外触发 error 事件。"""
                try:
                    # Event 没有 items() 方法，通过 payload 拷贝并剔除不可序列化的
                    # turn 对象（TurnContext 含 asyncio.Event 等无法 json.dumps）
                    payload = dict(evt.payload)
                    payload.pop("turn", None)
                    turn = evt.get("turn")
                    if turn is not None:
                        payload["session_id"] = getattr(turn, "session_id", "")
                        payload["source"] = getattr(turn, "source", "")
                        payload["success"] = bool(getattr(turn, "result", None))
                        payload["error"] = getattr(turn, "error", "") or ""
                    # 总是触发 turn_completed 类 webhook
                    await webhook_trigger.trigger_all("turn_completed", payload)
                    # 失败时额外触发 error 类 webhook（webhook name/id 含 "error"）
                    if payload.get("error"):
                        await webhook_trigger.trigger_all("error", payload)
                except Exception as exc:
                    logger.debug("webhook trigger failed for turn_completed: %s", exc)

            async def _on_approval_needed(evt):
                """approval_needed 事件触发 webhook。"""
                try:
                    payload = dict(evt.payload)
                    await webhook_trigger.trigger_all("approval_needed", payload)
                except Exception as exc:
                    logger.debug("webhook trigger failed for approval_needed: %s", exc)

            self.bus.subscribe("turn_completed", _on_turn_completed)
            self.bus.subscribe("approval_needed", _on_approval_needed)
            logger.info("webhook_trigger subscribed to event bus (turn_completed, approval_needed)")
        except Exception as exc:
            logger.warning("webhook_trigger event bus subscription failed (non-fatal): %s", exc)

        # 启动 AsyncTaskScheduler — 后台任务/延迟任务/定时任务的执行引擎
        # 之前未启动，导致所有 schedule_delayed / schedule_background 的任务都是死的
        # （followup_check、异步通知、定时提醒等全部不工作，表现为"一问一答"）
        try:
            from core.task_scheduler import get_task_scheduler
            task_scheduler = get_task_scheduler()
            await task_scheduler.start()
            logger.info("AsyncTaskScheduler started (background/delayed tasks enabled)")
        except Exception as exc:
            logger.warning("AsyncTaskScheduler start failed (non-fatal): %s", exc)

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
        # The coordinator assigns turn.result directly in many places (slash
        # commands, skill dispatch, LLM success) and always publishes
        # ``turn_completed`` afterwards. Subscribe to that event to wake the
        # instant *this* turn finishes, instead of polling turn.result ~10×/s
        # (which previously burned ~1800 empty polls per 180s request).
        def _on_completed(evt) -> None:
            if evt.get("turn") is turn:
                turn.mark_done()

        self.bus.subscribe("turn_completed", _on_completed)
        self.bus.publish({
            "type": "user_message",
            "payload": {"turn": turn, "session_id": session_id},
            "source": source,
        })
        try:
            await asyncio.wait_for(turn.wait_done(), timeout=180)
        except asyncio.TimeoutError:
            pass
        finally:
            self.bus.unsubscribe("turn_completed", _on_completed)
        return turn.result if turn.result is not None else "[timeout]"

    async def chat_with_thinking(self, text: str, source: str = "api", session_id: str = "default") -> dict:
        """Like chat(), but returns {"reply": ..., "thinking": ...} with thinking process.

        Uses its own TurnContext to avoid cross-request race conditions.
        """
        from core.context import TurnContext
        logger.info("chat_with_thinking: start, text=%r, source=%s, session_id=%s", text[:80], source, session_id)
        turn = TurnContext(input_text=text, source=source, session_id=session_id)
        # See chat() for why we wait on an event instead of polling turn.result.
        def _on_completed(evt) -> None:
            if evt.get("turn") is turn:
                turn.mark_done()

        self.bus.subscribe("turn_completed", _on_completed)
        self.bus.publish({
            "type": "user_message",
            "payload": {"turn": turn, "session_id": session_id},
            "source": source,
        })
        logger.info("chat_with_thinking: published user_message event, waiting for turn_completed...")
        try:
            await asyncio.wait_for(turn.wait_done(), timeout=180)
        except asyncio.TimeoutError:
            logger.warning("chat_with_thinking: TIMEOUT after 180s, text=%r", text[:80])
            pass
        finally:
            self.bus.unsubscribe("turn_completed", _on_completed)
        reply = turn.result if turn.result is not None else "[timeout]"
        thinking_text = turn.meta.get("thinking", "")
        logger.info("chat_with_thinking: done, reply_len=%d, thinking_len=%d", len(reply), len(thinking_text))
        return {"reply": reply, "session_id": session_id, "thinking": thinking_text}

    async def stop(self) -> None:
        # Publish shutdown event
        try:
            self.bus.publish({"type": "shutdown", "timestamp": time.time()})
        except Exception:
            logger.debug("shutdown event publish failed", exc_info=True)
        # 修复：关闭 WebhookTrigger 的 httpx 连接池, 之前 close() 定义了但无调用方
        # 导致进程退出时 keepalive 连接挂在服务端, 频繁重启场景 fd 泄漏。
        try:
            from core.webhook_trigger import get_webhook_trigger
            await get_webhook_trigger().close()
        except Exception as exc:
            logger.warning("webhook trigger close failed: %s", exc)
        await self._pm.stop_all()
        await self.bus.stop()
        # v2.1.0 统一库收尾：WAL checkpoint 截断，保证此后直接复制
        # one_agent.db 单文件即完整（无需 -wal/-shm 伴随文件）
        try:
            from core.hub import close_hub
            close_hub()
        except Exception:
            logger.debug("hub close failed", exc_info=True)


# ============================================================
# Entry point
# ============================================================

async def _interactive(app: OneAgentApp) -> None:
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║  One-Agent v2 — 自然语言即可操作，输入 '帮助'   ║")
    print("╚══════════════════════════════════════════════╝")

    def _match_intent(text: str) -> Optional[str]:
        """从自然语言中匹配用户意图，返回命令名或 None。

        Uses LLM-based intent classification instead of keyword matching.
        """
        from utils.intent_classifier import get_classifier

        classifier = get_classifier()
        return classifier.classify_cli_command(text)

    session_id = "cli-session"
    while True:
        try:
            # 关键：用 asyncio.to_thread 包装同步 input()，避免阻塞 event loop。
            # 如果用同步 input()，restart skill 创建的后台 task（asyncio.sleep + os.execv）
            # 无法执行——event loop 被阻塞在 input() 上，直到用户再次输入才恢复。
            # 这就是"更新/重启需要输入两次"的根因。
            raw = await asyncio.to_thread(input, "one-agent> ")
            line = raw.strip()
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
            result = _process_settings_command(line, app.config, bus=app.bus)
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
                except Exception as exc:
                    logger.debug("provider hint extraction failed: %s", exc)
            try:
                result = await llm.rebuild_tiers(provider=prov)
            except Exception as exc:  # noqa: BLE001
                logger.error("rebuild_tiers error: %s", exc)
                continue
            if not result.get("ok"):
                logger.error("rebuild_tiers failed: %s", result.get('error'))
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


async def main(interactive: bool = True) -> None:
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
    # ONE_AGENT_SETUP=1 forces re-running the wizard even if keys
    # are already configured (e.g. to switch providers).
    try:
        from core.setup_wizard import setup_if_needed
        _force_setup = os.environ.get("ONE_AGENT_SETUP", "").strip() == "1"
        _setup_ran = setup_if_needed(force=_force_setup)
    except Exception as exc:  # noqa: BLE001
        _setup_ran = False
        logger.warning("setup wizard unavailable: %s", exc)

    # 修复：支持 ONE_AGENT_ENV 选 config 文件（dev/staging/prod）。
    # 之前 dev_config.yaml 等文件头注释 "使用方式：ONE_AGENT_ENV=dev one-agent"
    # 是空头支票——没有任何代码读 ONE_AGENT_ENV。现在 _get_config_path 会处理。
    cfg_path = _get_config_path()
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
        if interactive:
            await _interactive(app)
        else:
            # Serve mode — wait indefinitely for shutdown signal
            print("\n  One-Agent 服务已启动 (serve 模式)")
            print("  按 Ctrl+C 停止\n")
            stop_event = asyncio.Event()
            def _on_serve_signal():
                stop_event.set()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _on_serve_signal)
                except (NotImplementedError, OSError):
                    pass
            await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Clean exit on Ctrl+C or signal
        print("\n\n========================================")
        print("  👋 再见！One-Agent 已关闭")
        print("========================================\n")
    finally:
        await app.stop()



def cli():
    """Console-script entry point for ``one-agent`` command.

    Subcommands:
      one-agent            — 启动 agent（默认交互模式）
      one-agent setup      — 交互式配置向导
      one-agent version    — 显示版本号
      one-agent config     — 检查配置文件路径和状态
      one-agent serve      — 启动后台服务（无 CLI 交互）
      one-agent export     — 导出全部数据（zip 含统一库，可异地部署）
      one-agent import     — 从备份导入（--restore-db 整库还原）
      one-agent migrate    — 旧版分散库迁入统一库（--dry-run 预览）
      one-agent db         — 统一库维护（stats/check/vacuum/backup）
    """
    args = sys.argv[1:]

    if not args:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print()
            sys.exit(0)
        return

    cmd = args[0]
    rest = args[1:]

    if cmd in ("version", "-v", "--version"):
        _cmd_version()
    elif cmd in ("setup", "config", "init"):
        _cmd_setup()
    elif cmd in ("check", "doctor"):
        _cmd_doctor()
    elif cmd in ("serve", "start", "run"):
        try:
            asyncio.run(main(interactive=False))
        except KeyboardInterrupt:
            print()
            sys.exit(0)
    elif cmd == "export":
        _cmd_export(rest)
    elif cmd == "import":
        _cmd_import(rest)
    elif cmd == "migrate":
        _cmd_migrate(rest)
    elif cmd == "db":
        _cmd_db(rest)
    elif cmd in ("-h", "--help", "help"):
        _cmd_help()
    else:
        print(f"Unknown command: {cmd}")
        print("Run 'one-agent help' for usage.")
        sys.exit(1)


def _cmd_export(args):
    """导出全部数据：one-agent export <path.zip>"""
    if not args:
        print("用法: one-agent export <输出路径.zip>")
        sys.exit(1)
    from core.backup_export import DataExporter
    from core.hub import resolve_data_dir

    data_dir = resolve_data_dir()
    print(f"  导出数据目录: {data_dir}")
    result = DataExporter(data_dir=data_dir).export_all(args[0])
    if result.success:
        print(f"  ✓ 导出成功: {result.file_path} "
              f"({result.size_bytes} bytes, {result.duration_seconds:.2f}s)")
        for key, val in result.items_exported.items():
            print(f"    - {key}: {val}")
        print("  提示: 该 zip 含 one_agent.db，可直接用于新环境部署"
              "（one-agent import <zip> --restore-db）")
    else:
        print(f"  ✗ 导出失败: {result.error}")
        sys.exit(1)


def _cmd_import(args):
    """从备份导入：one-agent import <backup.zip> [--restore-db]"""
    if not args:
        print("用法: one-agent import <备份.zip> [--restore-db]")
        print("  --restore-db  用归档内 one_agent.db 整库还原（新环境部署）")
        sys.exit(1)
    path = args[0]
    restore_db = "--restore-db" in args[1:]
    from core.backup_export import DataImporter
    from core.hub import resolve_data_dir

    data_dir = resolve_data_dir()
    print(f"  目标数据目录: {data_dir}")
    if restore_db:
        print("  模式: 整库还原（现有库将另存为 one_agent.db.pre_import）")
    result = DataImporter(data_dir=data_dir).import_from_file(
        path, merge=True, restore_db=restore_db)
    if result.success:
        print(f"  ✓ 导入成功 ({result.duration_seconds:.2f}s)")
        for key, val in result.items_imported.items():
            print(f"    - {key}: {val}")
    else:
        print(f"  ✗ 导入失败: {result.error}")
        sys.exit(1)


def _cmd_migrate(args):
    """旧库迁移：one-agent migrate [--dry-run]"""
    dry_run = "--dry-run" in args
    from core.hub import migrate_legacy, resolve_data_dir

    data_dir = resolve_data_dir()
    print(f"  数据目录: {data_dir}")
    if dry_run:
        print("  预览模式（不写入、不改名）:\n")
    else:
        print("  执行迁移（幂等，可重复运行）:\n")
    report = migrate_legacy(data_dir, dry_run=dry_run)
    if not report:
        print("  没有待迁移的旧版分散库 ✓")
        return
    for line in report:
        print(f"    - {line}")
    if dry_run:
        print("\n  运行 'one-agent migrate' 执行实际迁移")


def _cmd_db(args):
    """统一库维护：one-agent db <stats|check|vacuum|backup|backups>"""
    from core.hub import resolve_data_dir

    sub = args[0] if args else "stats"
    data_dir = resolve_data_dir()

    if sub == "stats":
        from core.db_maintenance import db_stats
        info = db_stats(data_dir)
        print(f"  统一库: {info.get('path')}")
        if not info.get("exists"):
            print("  ✗ 数据库不存在（首次启动时创建）")
            return
        size = info.get("size_bytes", 0)
        print(f"  大小: {size} bytes ({size / 1024:.1f} KiB)  "
              f"WAL: {info.get('wal_bytes', 0)}  权限: {info.get('mode')}")
        print(f"  表数量: {info.get('table_count')}  "
              f"待迁移旧库: {len(info.get('legacy_pending', []))}")
        tables = info.get("tables", {})
        for name in sorted(tables):
            if tables[name] > 0:
                print(f"    {name}: {tables[name]}")
        backups = info.get("backups") or {}
        if backups.get("count"):
            latest = backups["latest"]
            print(f"  自动备份: {backups['count']} 份，最新 {latest['file']}")
        else:
            print("  自动备份: 无（每天 04:30 自动执行，见 scheduler 配置）")
    elif sub == "check":
        from core.db_maintenance import integrity_check
        result = integrity_check(data_dir)
        if result.get("ok"):
            print("  ✓ 数据库完整性检查通过 (quick_check)")
            wal = result.get("wal_bytes", 0)
            if wal > 10 * 1024 * 1024:
                print(f"  ⚠ WAL 达 {wal} bytes，建议 'one-agent db vacuum'")
        else:
            print(f"  ✗ 完整性问题: {result.get('error') or result.get('problems')}")
            sys.exit(1)
    elif sub == "vacuum":
        from core.db_maintenance import vacuum
        print("  执行 checkpoint + VACUUM（需独占访问，建议停服执行）...")
        result = vacuum(data_dir)
        if result.get("ok"):
            print(f"  ✓ {result['before_bytes']} → {result['after_bytes']} bytes "
                  f"(回收 {result['reclaimed_bytes']})")
        else:
            print(f"  ✗ VACUUM 失败: {result.get('error')}")
            sys.exit(1)
    elif sub == "backup":
        from core.db_maintenance import run_auto_backup
        result = run_auto_backup(data_dir)
        if result.get("ok"):
            enc = "已加密" if result.get("encrypted") else "明文"
            print(f"  ✓ 备份成功: {result['path']} "
                  f"({result['size_bytes']} bytes, {enc})")
        else:
            print(f"  ✗ 备份失败: {result.get('error')}")
            sys.exit(1)
    elif sub == "backups":
        from core.db_maintenance import list_backups
        backups = list_backups(data_dir)
        if not backups:
            print("  暂无自动备份")
            return
        for b in backups:
            print(f"  {b['file']}  {b['size_bytes']} bytes")
    else:
        print(f"未知子命令: {sub}")
        print("用法: one-agent db <stats|check|vacuum|backup|backups>")
        sys.exit(1)


def _cmd_version():
    """Show version information."""
    print(f"One-Agent v{__version__}")
    print(f"Python  {sys.version.split()[0]}")
    print(f"Config  {_get_config_path()}")


def _cmd_setup():
    """Interactive setup wizard."""
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║           One-Agent 设置向导                    ║")
    print("  ║           Setup Wizard                          ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()

    # Step 1: LLM API Key
    print("  第 1 步：设置 LLM API Key")
    print("  ───────────────────────")
    print("  1. SenseNova (商汤)     — 新用户免费额度")
    print("  2. DeepSeek (深度求索)  — 价格极低")
    print("  3. DashScope (阿里百炼)  — 新用户免费额度")
    print("  4. OpenAI")
    print("  5. Anthropic (Claude)")
    print("  6. Ollama (本地/免费)")
    print("  7. 跳过")
    print()

    provider_map = {
        "1": ("SENSENOVA_API_KEY", "sensenova", "SenseNova", ["deepseek-v4-flash", "sensenova-6.7-flash-lite"]),
        "2": ("DEEPSEEK_API_KEY", "deepseek", "DeepSeek", ["deepseek-chat", "deepseek-reasoner"]),
        "3": ("DASHSCOPE_API_KEY", "dashscope", "DashScope", ["qwen-plus", "qwen-max", "qwen-turbo"]),
        "4": ("OPENAI_API_KEY", "openai", "OpenAI", ["gpt-4o-mini", "gpt-4o"]),
        "5": ("ANTHROPIC_API_KEY", "anthropic", "Anthropic", ["claude-3-5-haiku-latest", "claude-sonnet-4.5-20250514"]),
        "6": ("OLLAMA_HOST", "ollama", "Ollama", ["qwen2.5:7b", "llama3.1:8b"]),
        "7": (None, None, None, []),
    }

    choice = _ask("请选择 [1-7, 默认 1]: ", "1")
    info = provider_map.get(choice)
    if info is None or info[0] is None:
        print("  已跳过。稍后可编辑 .env 文件添加。")
    else:
        env_var, provider_key, name, models = info
        if env_var == "OLLAMA_HOST":
            val = _ask("  Ollama 地址 [默认 http://localhost:11434]: ", "http://localhost:11434")
        else:
            val = _ask(f"  请输入 {name} API Key: ", "").strip()
            if not val:
                print("  未输入，已跳过。")
                name = None
        if val and name:
            _write_env(env_var, val)
            print(f"  ✓ 已保存 {env_var}")

            # Choose model
            print(f"\n  可用的 {name} 模型:")
            for i, m in enumerate(models, 1):
                mark = " (默认)" if i == 1 else ""
                print(f"    {i}. {m}{mark}")
            mc = _ask("  选择模型 [默认 1]: ", "1")
            try:
                idx = int(mc) - 1
                model = models[idx] if 0 <= idx < len(models) else models[0]
            except (ValueError, IndexError):
                model = models[0]

            _update_config_llm(provider_key, model)
            print(f"  ✓ 模型设置为: {provider_key}/{model}")

    # Step 2: Basic settings
    print("\n  第 2 步：基础设置")
    print("  ──────────────")
    lang = _ask("  界面语言 (zh/en) [默认 zh]: ", "zh")
    _update_config("agent.language", lang)

    tz = _ask("  时区 [默认 Asia/Shanghai]: ", "Asia/Shanghai")
    _update_config("agent.timezone", tz)

    # Step 3: Security
    print("\n  第 3 步：安全设置（可选）")
    print("  ──────────────────")
    pwd = _ask("  设置系统执行密码（回车跳过）: ", "")
    if pwd:
        from executors.system import SystemExecutor
        pwd_hash = SystemExecutor.hash_password(pwd)
        _update_config("security.system_executor_password", pwd_hash)
        print("  ✓ 密码已设置")

    # Step 4: Gateway selection
    print("\n  第 4 步：消息网关（空格多选，回车确认）")
    print("  ───────────────────────────────────────")
    print("  1. CLI 命令行     (默认)")
    print("  2. Web UI 网页界面")
    print("  3. Telegram")
    print("  4. 企业微信")
    print("  5. 钉钉")
    print("  6. 飞书")
    print("  7. Discord")
    print("  8. Slack")
    print("  9. 个人微信")
    gw = _ask("  选择网关 [默认 1]: ", "1")

    gw_map = {
        "1": ("cli", "CLI"),
        "2": ("web", "Web UI"),
        "3": ("telegram", "Telegram"),
        "4": ("wecom", "企业微信"),
        "5": ("dingtalk", "钉钉"),
        "6": ("feishu", "飞书"),
        "7": ("discord", "Discord"),
        "8": ("slack", "Slack"),
        "9": ("wechat_personal", "个人微信"),
    }
    for key in gw:
        if key in gw_map:
            gw_key, gw_name = gw_map[key]
            _update_config(f"gateways.{gw_key}.enabled", "true")
            print(f"  ✓ 已启用: {gw_name}")

    # Summary
    print()
    print("  ✅ 设置完成！")
    print()
    print(f"  配置文件: {_get_config_path()}")
    print(f"  环境变量: {ROOT / '.env'}")
    print()
    print("  启动命令:")
    print("    one-agent          # 交互模式")
    print("    one-agent serve    # 后台服务")
    print()


def _cmd_doctor():
    """Check system health and config status."""
    print()
    print("  🩺 One-Agent 健康检查")
    print("  ────────────────────")
    print()

    # Python version
    py_ok = sys.version_info >= (3, 10)
    print(f"  {'✓' if py_ok else '✗'}  Python {sys.version.split()[0]}", end="")
    print("" if py_ok else " (需要 3.10+)")

    # Config file
    cfg_path = _get_config_path()
    cfg_ok = Path(cfg_path).exists()
    print(f"  {'✓' if cfg_ok else '✗'}  配置文件: {cfg_path}")

    # .env file
    env_ok = (ROOT / ".env").exists()
    print(f"  {'✓' if env_ok else '○'}  .env 文件: {ROOT / '.env'}")

    # API keys
    print()
    print("  API Keys:")
    key_vars = ["SENSENOVA_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
                "OLLAMA_HOST"]
    found = 0
    for var in key_vars:
        val = os.environ.get(var, "")
        if (ROOT / ".env").exists():
            # Also check .env
            try:
                with open(ROOT / ".env") as f:
                    for line in f:
                        if line.strip().startswith(f"{var}=") and "=" in line:
                            v = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                            if v:
                                val = v
                            break
            except Exception as exc:
                logger.debug("config read failed: %s", exc)
        if val:
            if val.startswith("http"):
                status = "✓"
            elif len(val) >= 20:
                status = f"✓ ({val[:4]}...{val[-4:]})"
            else:
                status = "✓ (*** masked ***)"
            print(f"    {status}  {var}")
            found += 1
        else:
            print(f"    ○  {var}  (未设置)")

    if found == 0:
        print("\n  ⚠  未配置任何 API Key，运行 'one-agent setup' 设置")

    # Data dir（v2.2.0：统一走 resolve_data_dir，与运行时一致）
    from core.hub import resolve_data_dir
    data_dir = Path(resolve_data_dir())
    print(f"\n  {'✓' if data_dir.exists() else '○'}  数据目录: {data_dir}")

    # Unified database（v2.2.0：doctor 补齐统一库体检）
    db_file = data_dir / "one_agent.db"
    if db_file.exists():
        from core.db_maintenance import db_stats, integrity_check
        check = integrity_check(str(data_dir))
        if check.get("ok"):
            print(f"  ✓  统一数据库: {db_file.name} "
                  f"({db_file.stat().st_size} bytes, quick_check 通过)")
        else:
            print(f"  ✗  统一数据库完整性异常: "
                  f"{check.get('error') or check.get('problems')}")
        stats = db_stats(str(data_dir))
        pending = stats.get("legacy_pending") or []
        if pending:
            print(f"  ⚠  待迁移旧库 {len(pending)} 个"
                  f"（运行 'one-agent migrate --dry-run' 预览）")
        backups = (stats.get("backups") or {})
        if backups.get("count"):
            print(f"  ✓  自动备份: {backups['count']} 份，"
                  f"最新 {backups['latest']['file']}")
        else:
            print("  ○  自动备份: 尚无（服务启动后每天 04:30 自动执行）")
    else:
        print("  ○  统一数据库: 尚未创建（首次启动时自动建立）")

    print()
    print("  运行 'one-agent setup' 修改配置；'one-agent db stats' 查看数据库")
    print()


def _cmd_help():
    """Show help message."""
    print()
    print(f"  One-Agent v{__version__}")
    print()
    print("  用法:")
    print("    one-agent              启动 agent（交互模式）")
    print("    one-agent setup        交互式配置向导")
    print("    one-agent serve        启动后台服务（无 CLI）")
    print("    one-agent version      显示版本号")
    print("    one-agent doctor       健康检查")
    print()
    print("  数据管理:")
    print("    one-agent export <zip>       导出全部数据（含统一库）")
    print("    one-agent import <zip>       从备份导入（--restore-db 整库还原）")
    print("    one-agent migrate [--dry-run] 旧版分散库迁入统一库")
    print("    one-agent db stats          统一库统计（表/大小/备份）")
    print("    one-agent db check          完整性检查（quick_check）")
    print("    one-agent db vacuum         回收空间（checkpoint+VACUUM）")
    print("    one-agent db backup         立即备份一次")
    print("    one-agent help         显示此帮助")
    print()
    print("  配置文件:")
    print(f"    {_get_config_path()}")
    print()
    print("  环境变量:")
    print(f"    {ROOT / '.env'}")
    print()


def _ask(prompt: str, default: str = "") -> str:
    """Ask a question, return user's answer or default."""
    try:
        ans = input(f"  {prompt}")
    except (EOFError, KeyboardInterrupt):
        return default
    return ans.strip() or default


def _get_config_path() -> str:
    """Get the active config file path.

    优先级：
    1. ONE_AGENT_CONFIG 环境变量（显式指定，最高优先级）
    2. ONE_AGENT_ENV 环境变量 → config/{env}_config.yaml
       （支持 dev/staging/prod，对应 dev_config.yaml 等文件头的
       "使用方式：ONE_AGENT_ENV=dev one-agent" 注释，之前该注释
       是空头支票——没有任何代码读 ONE_AGENT_ENV）
    3. config/default_config.yaml（兜底默认）
    """
    explicit = os.environ.get("ONE_AGENT_CONFIG")
    if explicit:
        return explicit
    env_name = os.environ.get("ONE_AGENT_ENV", "").strip().lower()
    if env_name:
        env_path = ROOT / "config" / f"{env_name}_config.yaml"
        if env_path.exists():
            return str(env_path)
    return str(ROOT / "config" / "default_config.yaml")


def _write_env(key: str, value: str) -> None:
    """Write an environment variable to .env file."""
    env_path = ROOT / ".env"
    env_path.touch(exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.stat().st_size > 0 else []
    updated = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)


def _update_config(key_path: str, value: str) -> None:
    """Update a nested config value by dot path (e.g. "agent.language")."""
    cfg_path = Path(_get_config_path())
    if not cfg_path.exists():
        return
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return

    keys = key_path.split(".")
    d = cfg
    for k in keys[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]

    # Try to keep type: bool stays bool, int stays int
    if value.lower() in ("true", "yes", "on"):
        d[keys[-1]] = True
    elif value.lower() in ("false", "no", "off"):
        d[keys[-1]] = False
    else:
        try:
            d[keys[-1]] = int(value)
        except (ValueError, TypeError):
            try:
                d[keys[-1]] = float(value)
            except (ValueError, TypeError):
                d[keys[-1]] = value

    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _update_config_llm(provider: str, model: str) -> None:
    """Update LLM provider and model in config."""
    cfg_path = Path(_get_config_path())
    if not cfg_path.exists():
        return
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return

    if "llm" not in cfg:
        cfg["llm"] = {}
    cfg["llm"]["primary_provider"] = provider
    cfg["llm"]["primary_model"] = f"{provider}/{model}"

    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


if __name__ == "__main__":
    cli()
