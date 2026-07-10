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

import asyncio
import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

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
        # 跟踪本插件订阅的所有事件，stop 时自动 unsubscribe
        # 避免子类遗忘清理导致内存泄漏或幽灵调用
        self._subscribed_handlers: List[Tuple[str, Any]] = []

    # lifecycle -------------------------------------------------------------
    async def setup(self, ctx: "AgentContext") -> None:
        self.ctx = ctx
        self.bus = ctx.bus
        logger.info("%s setup", self.name)

    async def start(self) -> None:
        logger.info("%s started", self.name)

    async def stop(self) -> None:
        # 自动取消所有通过 self.subscribe() 注册的事件订阅
        if self.bus is not None and self._subscribed_handlers:
            for event_type, handler in self._subscribed_handlers:
                try:
                    self.bus.unsubscribe(event_type, handler)
                except Exception as exc:
                    logger.debug("ignored non-critical error: %s", exc)
            self._subscribed_handlers.clear()
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

    def subscribe(self, event_type: str, handler) -> None:
        """订阅事件并自动跟踪，stop 时自动 unsubscribe。

        推荐插件使用此方法而非直接调用 bus.subscribe()，
        避免忘记清理导致内存泄漏和幽灵调用。
        """
        if self.bus is None:
            return
        self.bus.subscribe(event_type, handler)
        self._subscribed_handlers.append((event_type, handler))


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
            and attr.__name__ not in exclude_set
        ):
            seen.add(attr)
            try:
                pm.register(attr())
                logger.info("auto-discovered plugin: %s", attr.__name__)
            except (TypeError, ValueError, RuntimeError) as exc:
                logger.error("failed to instantiate plugin %s: %s", attr.__name__, exc, exc_info=True)


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

    async def setup_all(self, ctx: "AgentContext") -> tuple[int, int]:
        ordered = self._topological(self._plugins)
        success = 0
        failed = 0
        for plugin in ordered:
            try:
                await plugin.setup(ctx)
                success += 1
            except Exception:
                logger.exception("plugin %s setup failed", plugin.name)
                failed += 1
        logger.info("%d plugins set up, %d failed (priority-sorted)", success, failed)
        return success, failed

    async def start_all(self) -> tuple[int, int]:
        ordered = self._topological(self._plugins)
        success = 0
        failed = 0
        for plugin in ordered:
            try:
                await plugin.start()
                success += 1
            except Exception:
                logger.exception("plugin %s start failed", plugin.name)
                failed += 1
        logger.info("%d plugins started, %d failed", success, failed)
        return success, failed

    async def stop_all(self) -> tuple[int, int]:
        success = 0
        failed = 0
        # 停止顺序应与启动顺序相反（反拓扑序），而非 reversed(注册序)。
        # 注册序不等于拓扑序：如果依赖插件（如 llm）在注册序里靠后，
        # reversed 后会先 stop，依赖它的插件（如 router）的 stop 里
        # 若访问 self._llm 会拿到已关闭的 client，抛 RuntimeError。
        ordered = self._topological(self._plugins)
        for plugin in reversed(ordered):
            try:
                await plugin.stop()
                success += 1
            except Exception:
                logger.exception("plugin %s stop failed", plugin.name)
                failed += 1
        logger.info("%d plugins stopped, %d failed", success, failed)
        return success, failed

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

        for p in sorted(plugins, key=lambda pp: -pp.load_priority):
            visit(p, set())
        return ordered
