"""Minimal plugin system.

Each component (router, memory, gateways...) exposes itself as a Plugin so
the microkernel can wire it into the bus at boot time and tear it down on
shutdown.  The interface is intentionally small — we don't need dependency
injection, just a lifecycle contract.
"""

from __future__ import annotations

import logging
from typing import List

from .context import AgentContext
from .events import EventBus

logger = logging.getLogger(__name__)


class Plugin:
    """Base class for every subsystem.

    Subclasses override ``setup`` to publish/subscribe on the event bus,
    ``start``/``stop`` for long-running tasks.  The plugin manager calls
    these in dependency order.
    """

    name: str = "plugin"
    depends_on: List[str] = []

    def __init__(self) -> None:
        self.ctx: AgentContext | None = None
        self.bus: EventBus | None = None

    # lifecycle -------------------------------------------------------------
    async def setup(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self.bus = ctx.bus
        logger.info("%s setup", self.name)

    async def start(self) -> None:
        logger.info("%s started", self.name)

    async def stop(self) -> None:
        logger.info("%s stopped", self.name)

    # helpers ---------------------------------------------------------------
    def publish(self, event_type: str, **payload) -> None:
        if self.bus is None:
            return
        self.bus.publish({
            "type": event_type,
            "payload": payload,
            "source": self.name,
            "context_id": payload.get("context_id"),
        })


class PluginManager:
    """Loads & orchestrates plugins in topological order."""

    def __init__(self) -> None:
        self._plugins: List[Plugin] = []

    def register(self, plugin: Plugin) -> None:
        self._plugins.append(plugin)

    async def setup_all(self, ctx: AgentContext) -> None:
        # naive ordering — acceptable because plugins declare deps explicitly
        ordered = self._topological(self._plugins)
        for plugin in ordered:
            await plugin.setup(ctx)
        logger.info("%d plugins set up", len(ordered))

    async def start_all(self) -> None:
        for plugin in self._plugins:
            await plugin.start()

    async def stop_all(self) -> None:
        for plugin in reversed(self._plugins):
            try:
                await plugin.stop()
            except Exception:
                logger.exception("failed to stop %s", plugin.name)

    # ---------------------------------------------------------------- utils
    @staticmethod
    def _topological(plugins: List[Plugin]) -> List[Plugin]:
        name_to_plugin = {p.name: p for p in plugins}
        ordered: List[Plugin] = []
        visited = set()

        def visit(p: Plugin, stack: set) -> None:
            if p.name in visited:
                return
            if p.name in stack:
                raise RuntimeError(f"circular dep around {p.name}")
            stack.add(p.name)
            for dep in p.depends_on:
                if dep in name_to_plugin:
                    visit(name_to_plugin[dep], stack)
            stack.discard(p.name)
            visited.add(p.name)
            ordered.append(p)

        for p in plugins:
            visit(p, set())
        return ordered
