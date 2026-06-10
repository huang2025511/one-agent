"""Minimal plugin system.

Each component (router, memory, gateways...) exposes itself as a Plugin so
the microkernel can wire it into the bus at boot time and tear it down on
shutdown.  The interface is intentionally small — we don't need dependency
injection, just a lifecycle contract.

Enhanced with:
  - Auto-discovery from package directories
  - Priority-based loading order
  - Dependency graph validation
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Type

if TYPE_CHECKING:
    from core.context import AgentContext

logger = logging.getLogger(__name__)


class Plugin:
    """Base class for every subsystem.

    Subclasses override ``setup`` to publish/subscribe on the event bus,
    ``start``/``stop`` for long-running tasks.  The plugin manager calls
    these in dependency order.
    """

    name: str = "plugin"
    depends_on: List[str] = []
    load_priority: int = 0  # Higher = loaded first

    def __init__(self) -> None:
        self.ctx: Optional["AgentContext"] = None
        self.bus: Optional[Any] = None

    # lifecycle -------------------------------------------------------------
    async def setup(self, ctx: "AgentContext") -> None:
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


def _collect_from_module(
    module,
    pkg_name: str,
    pm: "PluginManager",
    seen: set,
    exclude_set: set,
) -> None:
    """Extract Plugin subclasses from a module and register them."""
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, Plugin)
            and attr is not Plugin
            and attr not in seen
        ):
            seen.add(attr)
            pm.register(attr())
            logger.debug("auto-discovered plugin: %s", attr.__name__)


class PluginManager:
    """Loads & orchestrates plugins with auto-discovery and topological ordering."""

    def __init__(self) -> None:
        self._plugins: List[Plugin] = []

    def register(self, plugin: Plugin) -> None:
        self._plugins.append(plugin)

    @classmethod
    def discover(

        cls,
        package_paths: List[str],
        exclude: Optional[List[str]] = None,
    ) -> "PluginManager":
        """Auto-discover Plugin subclasses from given package directories.

        For each package dir, we look for the package itself (for its __init__)
        AND for each .py sub-module in the directory — both may define plugins.

        Example:
            pm = PluginManager.discover([
                "router", "memory", "skills",
                "executors", "gateways", "scheduler",
            ])
        """
        pm = cls()
        exclude_set = set(exclude or [])
        seen: set[str] = set()

        for pkg_name in package_paths:
            # First, try to import the package __init__ (in case it defines plugins)
            try:
                pkg_module = importlib.import_module(pkg_name)
                _collect_from_module(pkg_module, pkg_name, pm, seen, exclude_set)
            except ImportError as exc:
                logger.warning("could not import package %s: %s", pkg_name, exc)
                continue

            # Then scan the package directory for sub-modules
            try:
                pkg = importlib.import_module(pkg_name)
                pkg_path = Path(pkg.__file__).parent
            except Exception as exc:
                logger.warning("could not resolve path for %s: %s", pkg_name, exc)
                continue

            for file in sorted(pkg_path.glob("*.py")):
                stem = file.stem
                if stem.startswith("_") or stem in exclude_set:
                    continue
                try:
                    # e.g. "executors.shell" if pkg_name is "executors"
                    module = importlib.import_module(f"{pkg_name}.{stem}")
                    _collect_from_module(module, pkg_name, pm, seen, exclude_set)
                except ImportError as exc:
                    logger.warning("could not import %s.%s: %s", pkg_name, stem, exc)

        return pm

    async def setup_all(self, ctx: "AgentContext") -> None:
        ordered = self._topological(self._plugins)
        for plugin in ordered:
            await plugin.setup(ctx)
        logger.info("%d plugins set up (priority-sorted)", len(ordered))

    async def start_all(self) -> None:
        for plugin in self._plugins:
            await plugin.start()

    async def stop_all(self) -> None:
        for plugin in reversed(self._plugins):
            try:
                await plugin.stop()
            except Exception:
                logger.exception("failed to stop %s", plugin.name)

    def get_by_name(self, name: str) -> Optional[Plugin]:
        for p in self._plugins:
            if p.name == name:
                return p
        return None

    # ---------------------------------------------------------------- utils
    @staticmethod
    def _topological(plugins: List[Plugin]) -> List[Plugin]:
        """Sort plugins by dependency order, then by load_priority descending."""
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

        # Secondary sort: load_priority descending
        ordered.sort(key=lambda p: -p.load_priority)
        return ordered
