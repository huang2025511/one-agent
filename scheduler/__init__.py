"""Cron-style task scheduler — OpenClaw/Hermes proactive behaviour.

Uses APScheduler for the heavy lifting.  Exposes a tiny API for other
plugins to register jobs, plus reads jobs from a YAML file at boot.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
    _HAS_AP = True
except Exception:  # noqa: BLE001
    _HAS_AP = False

from core.plugin import Plugin

logger = logging.getLogger(__name__)


class SchedulerPlugin(Plugin):
    """Proactive task driver.

    Without any scheduler the agent is strictly reactive — it only responds
    to user messages.  This plugin fires "cron" events at configured
    intervals so other plugins can do work without a user message.
    """

    name = "scheduler"

    def __init__(self) -> None:
        super().__init__()
        self._scheduler: Optional["AsyncIOScheduler"] = None
        self._enabled = True
        self._jobs_file: Optional[str] = None

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("scheduler") or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._jobs_file = cfg.get("user_jobs_file")
        if not self._enabled or not _HAS_AP:
            logger.info("scheduler disabled (has_apscheduler=%s)", _HAS_AP)
            return
        self._scheduler = AsyncIOScheduler(timezone=cfg.get("timezone"))
        for job in cfg.get("builtin_jobs", []) or []:
            if not job.get("enabled", True):
                continue
            self._register_cron_event(job["name"], job.get("cron", "0 * * * *"))

    async def start(self) -> None:
        if self._scheduler is not None:
            self._scheduler.start()
            logger.info("scheduler started (%d jobs)", len(self._scheduler.get_jobs()))

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
        await super().stop()

    # ------------------------------------------------------------ public
    def add_cron(self, cron: str, func: Callable[..., Any], name: str) -> None:
        if self._scheduler is None:
            return
        minute, hour, day, month, day_of_week = cron.split()
        self._scheduler.add_job(
            func, "cron",
            minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
            id=name, replace_existing=True,
        )

    # ------------------------------------------------------------ internal
    def _register_cron_event(self, name: str, cron: str) -> None:
        def fire():
            if self.bus is not None:
                self.bus.publish({  # type: ignore[attr-defined]
                    "type": "cron",
                    "name": name,
                })
            logger.debug("cron fired: %s", name)
        self.add_cron(cron, fire, name)
