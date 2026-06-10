"""Athena — bootstrap & main entry point.

Wire up every subsystem, then hand control to the CLI gateway or the web
UI.  Run with ``python athena.py`` (or ``python -m athena``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# make local package imports work without installing
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.agent import load_config  # noqa: E402
from core.context import AgentContext  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.plugin import PluginManager  # noqa: E402
from core.coordinator import Coordinator  # noqa: E402
from models import LLMProvider  # noqa: E402
from router import SmartRouter, HistoryRecorder  # noqa: E402
from memory import MemoryPlugin  # noqa: E402
from skills import SkillManager  # noqa: E402
from executors import ShellExecutor, DockerExecutor, BrowserExecutor  # noqa: E402
from gateways import CLIGateway, TelegramGateway, WebGateway  # noqa: E402
from scheduler import SchedulerPlugin  # noqa: E402


logging.basicConfig(
    level=getattr(logging, os.environ.get("ATHENA_LOGLEVEL", "INFO")),
    format="%(asctime)s | %(levelname)7s | %(name)s | %(message)s",
)
logger = logging.getLogger("athena")


# ------------------------------------------------------------------ helpers
class AthenaApp:
    """Top-level assembly: builds plugin manager, coordinates plugins."""

    def __init__(self, config_path: str) -> None:
        self.config = load_config(config_path)
        self.bus = EventBus()
        # a tiny EventBus wrapper that also accepts dict-style publishes —
        # plugins write self.publish("event_type", key=value)
        self._pm = PluginManager()
        data_dir = self.config.get("agent", {}).get("data_dir", "./data")
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        # register plugins in topological order (deps first)
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
        self.web = WebGateway()

        for p in (
            self.llm, self.router, self.history, self.memory, self.skills,
            self.exec_shell, self.exec_docker, self.exec_browser,
            self.coordinator, self.scheduler, self.cli, self.telegram, self.web,
        ):
            self._pm.register(p)

        self.ctx: AgentContext | None = None

    async def start(self) -> None:
        self.ctx = AgentContext(config=self.config, bus=self.bus, data_dir=self.config.get("agent", {}).get("data_dir", "./data"))
        # expose plugin registry on context so plugins can find each other
        self.ctx._plugins = [  # type: ignore[attr-defined]
            self.llm, self.router, self.history, self.memory, self.skills,
            self.exec_shell, self.exec_docker, self.exec_browser,
            self.coordinator, self.scheduler, self.cli, self.telegram, self.web,
        ]
        # quick fix so plugins can access the list of installed plugins by name
        await self.bus.start()
        await self._pm.setup_all(self.ctx)
        # cross-bind plugins that need direct references
        self.router.bind_llm(self.llm)
        self.coordinator.bind(self.llm, self.skills)
        self.web.bind_callback(self.chat)
        await self._pm.start_all()

    async def chat(self, text: str, source: str = "cli", session_id: str = "default") -> str:
        """Programmatic entry point."""
        from core.context import TurnContext
        turn = TurnContext(input_text=text, source=source, session_id=session_id)
        # publish user_message → router classifies & publishes turn_routed → coordinator replies
        self.bus.publish({
            "type": "user_message",
            "payload": {"turn": turn, "session_id": session_id},
            "source": source,
        })
        # wait for result via polling — plugins eventually set turn.result
        deadline = asyncio.get_event_loop().time() + 180
        while asyncio.get_event_loop().time() < deadline:
            if turn.result is not None or turn.error is not None:
                break
            await asyncio.sleep(0.1)
        return turn.result or f"[timeout]"

    async def stop(self) -> None:
        await self._pm.stop_all()
        await self.bus.stop()


async def _interactive(app: AthenaApp) -> None:
    """Hand-written REPL — no heavy readline dependency."""
    print()
    print("╔══════════════════════════════════════════╗")
    print("║  Athena — enter a message, or 'exit'     ║")
    print("╚══════════════════════════════════════════╝")
    session_id = "cli-session"
    while True:
        try:
            line = input("athena> ").strip()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print()
            continue
        if not line:
            continue
        if line.lower() in {"exit", "quit", "q"}:
            return
        if line.lower() in {"help", "?"}:
            print("Commands: exit | skills | status | stats")
            continue
        if line.lower() == "skills":
            print("loaded:", ", ".join(app.skills.all_skill_ids()) or "(none)")
            continue
        if line.lower() == "status":
            print(app.memory.stats() if app.memory else "(no memory)")
            print(app.llm.stats() if app.llm else "(no llm)")
            continue
        try:
            reply = await asyncio.wait_for(app.chat(line, source="cli", session_id=session_id), timeout=180)
        except asyncio.TimeoutError:
            print("[timeout — try again]")
            continue
        print(reply)


async def main() -> None:
    cfg_path = os.environ.get("ATHENA_CONFIG", str(ROOT / "config" / "default_config.yaml"))
    if not Path(cfg_path).exists():
        logger.error("config not found: %s", cfg_path)
        sys.exit(2)
    app = AthenaApp(cfg_path)
    await app.start()
    try:
        await _interactive(app)
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
