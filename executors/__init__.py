"""Code execution backends — shell, docker, browser.

Security-first: the shell executor uses regex patterns instead of simple
command lists.  Docker runs with full security hardening (no network,
non-root user, dropped capabilities).  Browser uses httpx headless fetcher.

Enhanced with:
  - Regex command allow-list for shell
  - Docker security hardening (cap-drop, read-only, user 1000:1000)
  - Structured audit log entries
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# Regex patterns for allowed shell commands (much safer than word lists)
ALLOWED_PATTERNS = {
    # python: run a .py file, optionally with arguments
    "python": r"^python3?\s+[\w./-]+\.py(\s+[\w./=-]+)*$",
    # node: run node scripts
    "node": r"^node\s+[\w./-]+\.js(\s+[\w./=-]+)*$",
    # git: safe read-only operations only
    "git": r"^git\s+(clone|pull|status|log|diff|show|ls-files|rev-parse)\s+[\w./:@#-]+$",
    # curl: fetch remote content (no file upload / POST data with secrets)
    "curl": r"^curl\s+(-[sSLIo]|--silent|--show-error|--location)?\s*https?://[\w./:@\-_?=&]+(/[\w./:@\-_?=&-]*)*(\?[\w=&]+)?$",
    # ls / cat / grep / find: safe read operations
    "ls": r"^ls(\s+(-l|-a|-R|--color=auto)[\w./-]*)*\s+[\w./-]*$",
    "cat": r"^cat\s+[\w./-]+$",
    "grep": r"^grep\s+(-i|-n|-r|--color=auto)?\s+['\"][\w\s./-]+['\"]?\s+[\w./-]+$",
    "find": r"^find\s+[\w./-]+\s+(-type[fd]|-name|'-name')\s+['\"][\w.*]+['\"](\s+(-type[fd]|-name|'-name')\s+['\"][\w.*]+['\"])*\s*$",
    # echo / date / uptime: info only
    "echo": r"^echo\s+['\"][^'\"`$]+['\"]?\s*$",
    "date": r"^date\s*(\s+-u)?$",
    "uptime": r"^uptime\s*$",
    "free": r"^free\s*(-h)?$",
    "df": r"^df\s*(-h)?\s*[\w./]*$",
}


class ShellExecutor(Plugin):
    """Runs shell commands locally with regex allow-list security."""

    name = "executor_shell"

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._timeout = 60
        self._workdir = "./data/workspace"
        self._audit_log_path = "./data/logs/executor_audit.log"

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("execution") or {}).get("local_shell") or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._timeout = int(cfg.get("default_timeout", 60))
        self._workdir = cfg.get("workdir", self._workdir)
        Path(self._workdir).mkdir(parents=True, exist_ok=True)
        Path(self._audit_log_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "shell executor enabled=%s patterns=%d",
            self._enabled,
            len(ALLOWED_PATTERNS),
        )

    def can_run(self, command: str) -> bool:
        if not self._enabled:
            return False
        import re
        for name, pattern in ALLOWED_PATTERNS.items():
            if re.fullmatch(pattern, command.strip()):
                return True
        return False

    def _audit(self, command: str, result: Dict) -> None:
        """Append structured audit entry."""
        import json
        entry = {
            "t": time.time(),
            "command": command[:200],
            "returncode": result.get("returncode"),
            "stdout_size": len(result.get("stdout", "")),
            "stderr_size": len(result.get("stderr", "")),
            "allowed": self.can_run(command),
        }
        try:
            with open(self._audit_log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def run(
        self,
        command: str,
        timeout: Optional[int] = None,
        audit: bool = True,
    ) -> Dict:
        if not self.can_run(command):
            result = {
                "stdout": "",
                "stderr": f"[security] command not in allow-list or executor disabled: {command[:100]}",
                "returncode": -1,
                "blocked": True,
            }
            self._audit(command, result)
            return result

        try:
            out = subprocess.run(
                command,
                shell=True,
                cwd=self._workdir,
                timeout=timeout or self._timeout,
                capture_output=True,
                text=True,
            )
            result = {
                "stdout": (out.stdout or "")[:8000],
                "stderr": (out.stderr or "")[:4000],
                "returncode": out.returncode,
                "blocked": False,
            }
        except subprocess.TimeoutExpired:
            result = {
                "stdout": "",
                "stderr": f"[timeout after {timeout or self._timeout}s]",
                "returncode": -2,
                "blocked": False,
            }
        except Exception as exc:  # noqa: BLE001
            result = {"stdout": "", "stderr": str(exc), "returncode": -3, "blocked": False}

        if audit:
            self._audit(command, result)
        return result


class DockerExecutor(Plugin):
    """Runs commands in a hardened Docker container.

    Security hardening:
      --network=none   — no network access
      --read-only      — read-only filesystem
      --user=1000:1000 — non-root user
      --cap-drop=all   — drop all Linux capabilities
      --security-opt=no-new-privileges
    """

    name = "executor_docker"

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._image = "python:3.12-slim"
        self._timeout = 120
        self._mem_limit_mb = 512
        self._cpu_quota = 50000  # 50% of one CPU

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("execution") or {}).get("docker") or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._image = cfg.get("image", self._image)
        self._timeout = int(cfg.get("timeout", 120))
        self._mem_limit_mb = int(cfg.get("memory_limit_mb", 512))
        self._cpu_quota = int(cfg.get("cpu_quota", 50000))
        logger.info(
            "docker executor enabled=%s image=%s mem=%dMB cpu_quota=%d",
            self._enabled, self._image, self._mem_limit_mb, self._cpu_quota,
        )

    def run(self, command: str) -> Dict:
        if not self._enabled:
            return {
                "stdout": "", "stderr": "docker executor disabled",
                "returncode": -1, "blocked": False,
            }
        args = [
            "docker", "run", "--rm",
            f"--memory={self._mem_limit_mb}m",
            f"--cpu-quota={self._cpu_quota}",
            "--network=none",
            "--read-only",
            "--user=1000:1000",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            self._image,
            "sh", "-c", command,
        ]
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=self._timeout)
            return {
                "stdout": (out.stdout or "")[:8000],
                "stderr": (out.stderr or "")[:4000],
                "returncode": out.returncode,
                "blocked": False,
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "docker timeout", "returncode": -2}
        except FileNotFoundError:
            return {"stdout": "", "stderr": "docker binary not found", "returncode": -3}
        except Exception as exc:  # noqa: BLE001
            return {"stdout": "", "stderr": str(exc), "returncode": -4}


class BrowserExecutor(Plugin):
    """Minimal browser automation: GET a page and return text / HTML."""

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
                headers={"User-Agent": "AthenaAgent/2.0 (+https://github.com/huang2025511/agnet)"},
            )
        logger.info("browser executor enabled=%s", self._enabled)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        await super().stop()

    async def fetch(self, url: str, max_chars: int = 8000) -> Dict:
        if not self._enabled or self._client is None:
            return {"status": 0, "text": "", "error": "browser executor disabled"}
        try:
            resp = await self._client.get(url)
            text = (resp.text or "")[:max_chars]
            return {
                "status": resp.status_code,
                "text": text,
                "content_type": resp.headers.get("content-type", ""),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": 0, "text": "", "error": str(exc)}
