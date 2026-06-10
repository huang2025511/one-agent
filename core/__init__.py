"""Athena Agent Core Package.

Microkernel event-driven architecture.
"""

from .events import EventBus, Event, EventPriority
from .context import AgentContext, TurnContext
from .agent import AthenaAgent
from .plugin import Plugin, PluginManager

__all__ = [
    "EventBus",
    "Event",
    "EventPriority",
    "AgentContext",
    "TurnContext",
    "AthenaAgent",
    "Plugin",
    "PluginManager",
]
