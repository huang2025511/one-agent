"""Top-level Athena agent bootstrap.

Wires up every subsystem, then hands control to the CLI gateway or the web
UI.  Run with ``python athena.py`` (or ``python -m athena``).

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

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.context import AgentContext  # noqa: E402
from core.plugin import PluginManager  # noqa: E402


# ============================================================
# Pydantic config models (validation at load time)
# ============================================================

class LLMApiKeys(BaseModel):
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
    name: str = "Athena"
    description: str = "Token-efficient self-evolving microkernel AI agent"
    version: str = "2.0.0"
    data_dir: str = "./data"
    log_level: str = Field(default="INFO")
    timezone: str = Field(default="UTC")

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
    enc_key = os.environ.get("ATHENA_ENCRYPTION_KEY")
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
        log_dir / "athena.log",
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
# AthenaApp
# ============================================================

class AthenaApp:
    """Top-level assembly: builds plugin manager, coordinates plugins."""

    def __init__(self, config_path: str) -> None:
        from core.events import EventBus
        from core.coordinator import Coordinator
        from models import LLMProvider
        from router import SmartRouter, HistoryRecorder
        from memory import MemoryPlugin
        from skills import SkillManager
        from executors import ShellExecutor, DockerExecutor, BrowserExecutor
        from gateways import CLIGateway, TelegramGateway, WeComGateway, WebGateway
        from scheduler import SchedulerPlugin
        from multimodal import MultimodalPlugin
        from api import RESTAPIGateway
        from monitor import MonitoringPlugin
        from marketplace import MarketplacePlugin

        self.config = load_config(config_path)
        setup_logging(self.config)  # type: ignore[arg-type]

        self.bus = EventBus()

        self.llm = LLMProvider()
        self.router = SmartRouter()
        self.history = HistoryRecorder()
        self.memory = MemoryPlugin()
        self.skills = SkillManager()
        self.exec_shell = ShellExecutor()
        self.exec_docker = DockerExecutor()
        self.exec_browser = BrowserExecutor()
        self.coordinator = Coordinator()
        self.scheduler = SchedulerPlugin()
        self.cli = CLIGateway()
        self.telegram = TelegramGateway()
        self.wecom = WeComGateway()
        self.web = WebGateway()
        self.multimodal = MultimodalPlugin()
        self.rest_api = RESTAPIGateway()
        self.monitor = MonitoringPlugin()
        self.marketplace = MarketplacePlugin()

        self._pm = PluginManager()
        for p in (
            self.llm, self.router, self.history, self.memory, self.skills,
            self.exec_shell, self.exec_docker, self.exec_browser,
            self.coordinator, self.scheduler,
            self.cli, self.telegram, self.wecom, self.web,
            self.multimodal, self.rest_api, self.monitor, self.marketplace,
        ):
            self._pm.register(p)

        self.ctx: Optional[AgentContext] = None

    async def start(self) -> None:
        self.ctx = AgentContext(
            config=self.config.model_dump(),
            bus=self.bus,
            data_dir=self.config.agent.data_dir,
        )
        self.ctx._plugins = [
            self.llm, self.router, self.history, self.memory, self.skills,
            self.exec_shell, self.exec_docker, self.exec_browser,
            self.coordinator, self.scheduler,
            self.cli, self.telegram, self.wecom, self.web,
            self.multimodal, self.rest_api, self.monitor, self.marketplace,
        ]

        await self.bus.start()
        await self._pm.setup_all(self.ctx)
        self.router.bind_llm(self.llm)
        self.coordinator.bind(self.llm, self.skills)
        self.web.bind_callback(self.chat)
        self.rest_api.bind_callback(self.chat)
        # wire marketplace to skills plugin
        self.marketplace._skills_plugin = self.skills  # type: ignore[attr-defined]
        await self._pm.start_all()

    async def chat(self, text: str, source: str = "cli", session_id: str = "default") -> str:
        from core.context import TurnContext
        turn = TurnContext(input_text=text, source=source, session_id=session_id)
        self.bus.publish({
            "type": "user_message",
            "payload": {"turn": turn, "session_id": session_id},
            "source": source,
        })
        deadline = asyncio.get_event_loop().time() + 180
        while asyncio.get_event_loop().time() < deadline:
            if turn.result is not None or turn.error is not None:
                break
            await asyncio.sleep(0.1)
        return turn.result or "[timeout]"

    async def stop(self) -> None:
        await self._pm.stop_all()
        await self.bus.stop()


# ============================================================
# Entry point
# ============================================================

async def _interactive(app: AthenaApp) -> None:
    print()
    print("╔══════════════════════════════════════════╗")
    print("║  Athena v2 — enter a message, or 'exit'  ║")
    print("╚══════════════════════════════════════════╝")
    session_id = "cli-session"
    while True:
        try:
            line = input("athena> ").strip()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print("\n(interrupted)")
            return
        if not line:
            continue
        cmd = line.lower()
        if cmd in {"exit", "quit", "q"}:
            return
        if cmd in {"help", "?"}:
            print("Commands: exit | skills | status | stats | metrics | dlq | bus")
            continue
        if cmd == "skills":
            print("loaded:", ", ".join(app.skills.all_skill_ids()) or "(none)")
            continue
        if cmd == "status":
            print("memory:", app.memory.stats() if app.memory else {})
            print("llm:", app.llm.stats() if app.llm else {})
            print("bus:", app.bus.metrics() if app.bus else {})
            continue
        if cmd == "metrics":
            print("bus metrics:", app.bus.metrics())
            print("llm calls:", app.llm.stats() if app.llm else {})
            print("skills loaded:", app.skills.all_skill_ids())
            print("uptime:", app.ctx.uptime() if app.ctx else 0)
            continue
        if cmd == "dlq":
            dlq = app.bus.get_dlq(10)
            print(f"dead-letter queue ({len(dlq)} items):")
            for e in dlq:
                print(f"  [{e.id}] {e.type} from {e.source}")
            continue
        if cmd == "bus":
            print("event types registered:")
            print("  (use bus.metrics() for full stats)")
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
    cfg_path = os.environ.get("ATHENA_CONFIG", str(ROOT / "config" / "default_config.yaml"))
    if not Path(cfg_path).exists():
        sys.exit(f"config not found: {cfg_path}")
    app = AthenaApp(cfg_path)
    await app.start()

    # register graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_event_loop()
    _shutdown_triggered = False

    def _on_signal():
        nonlocal _shutdown_triggered
        if not _shutdown_triggered:
            _shutdown_triggered = True
            print("\n[shutting down...]")
            asyncio.create_task(app.stop())

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
