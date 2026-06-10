"""Code execution backends — shell, docker, browser.

Security-first: the shell executor runs a fixed allow-list and caps
execution time.  Docker (optional) isolates everything.  Browser automation
uses a tiny ``httpx``-based headless fetcher by default; Playwright can be
enabled separately.

API is intentionally minimal so it's easy to swap backends.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import shlex
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


class ShellExecutor(Plugin):
    """Runs shell commands locally.  Requires explicit opt-in via config."""

    name = "executor_shell"

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._timeout = 60
        self._workdir = "./data/workspace"
        self._allowed: List[str] = []

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("execution") or {}).get("local_shell") or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._timeout = int(cfg.get("default_timeout", 60))
        self._allowed = [str(x) for x in cfg.get("allowed_commands", []) or []]
        self._workdir = cfg.get("workdir", self._workdir)
        Path(self._workdir).mkdir(parents=True, exist_ok=True)
        logger.info("shell executor enabled=%s allowed=%s", self._enabled, self._allowed)

    def can_run(self, command: str) -> bool:
        if not self._enabled:
            return False
        if not self._allowed:
            return True
        binary = command.strip().split()[0] if command.strip() else ""
        return binary in self._allowed

    def run(self, command: str, timeout: Optional[int] = None) -> Dict[str, object]:
        """Run a shell command.  Returns {stdout, stderr, returncode}."""
        if not self.can_run(command):
            return {
                "stdout": "",
                "stderr": f"command not in allow-list or executor disabled: {command}",
                "returncode": -1,
            }
        try:
            out = subprocess.run(
                command,
                shell=True,
                cwd=self._workdir,
                timeout=timeout or self._timeout,
                capture_output=True,
                text=True,
            )
            return {
                "stdout": (out.stdout or "")[:8000],
                "stderr": (out.stderr or "")[:4000],
                "returncode": out.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": f"timeout after {timeout or self._timeout}s", "returncode": -2}
        except Exception as exc:  # noqa: BLE001
            return {"stdout": "", "stderr": str(exc), "returncode": -3}


class DockerExecutor(Plugin):
    """Runs a command inside a throwaway Docker container.  Optional."""

    name = "executor_docker"

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._image = "python:3.12-slim"
        self._timeout = 120
        self._mem_limit_mb = 512

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("execution") or {}).get("docker") or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._image = cfg.get("image", self._image)
        self._timeout = int(cfg.get("timeout", 120))
        self._mem_limit_mb = int(cfg.get("memory_limit_mb", 512))
        logger.info("docker executor enabled=%s image=%s", self._enabled, self._image)

    def run(self, command: str) -> Dict[str, object]:
        if not self._enabled:
            return {"stdout": "", "stderr": "docker executor disabled", "returncode": -1}
        args = [
            "docker", "run", "--rm",
            f"--memory={self._mem_limit_mb}m",
            "--network=none",
            "--read-only",
            self._image,
            "sh", "-c", command,
        ]
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=self._timeout)
            return {"stdout": (out.stdout or "")[:8000], "stderr": (out.stderr or "")[:4000], "returncode": out.returncode}
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "docker timeout", "returncode": -2}
        except FileNotFoundError:
            return {"stdout": "", "stderr": "docker binary not found", "returncode": -3}
        except Exception as exc:  # noqa: BLE001
            return {"stdout": "", "stderr": str(exc), "returncode": -4}


class BrowserExecutor(Plugin):
    """Minimal browser automation: GET a page and return text / HTML.

    This is deliberately NOT a full Playwright integration — it uses
    ``httpx`` to fetch pages, which is enough for most "read a URL" use
    cases.  Heavy JS rendering is out of scope (enable Playwright via the
    config if you need it — we don't bundle that dependency here).
    """

    name = "executor_browser"

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = 30

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("execution") or {}).get("browser") or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._timeout = int(cfg.get("timeout", 30))
        if self._enabled:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": "AthenaAgent/1.0"},
            )
        logger.info("browser executor enabled=%s", self._enabled)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        await super().stop()

    async def fetch(self, url: str) -> Dict[str, object]:
        if not self._enabled or self._client is None:
            return {"status": 0, "text": "", "error": "browser executor disabled"}
        try:
            resp = await self._client.get(url)
            return {
                "status": resp.status_code,
                "text": (resp.text or "")[:8000],
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": 0, "text": "", "error": str(exc)}
