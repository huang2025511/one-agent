"""Code execution backends — shell, docker, browser, python.

Security-first: the shell executor uses regex patterns instead of simple
command lists.  Docker runs with full security hardening (no network,
non-root user, dropped capabilities).  Browser uses httpx headless fetcher.
Python executor provides sandboxed code execution.

Enhanced with:
  - Regex command allow-list for shell
  - Docker security hardening (cap-drop, read-only, user 1000:1000)
  - Structured audit log entries
  - Sandboxed Python REPL executor
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

from core.plugin import Plugin
from core.exceptions import SecurityError, InputValidationError
from executors.python_runner import PythonExecutor
from executors.system import SystemExecutor, classify_command  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = ["ShellExecutor", "DockerExecutor", "BrowserExecutor", "PythonExecutor", "SystemExecutor", "classify_command"]


# Shell metacharacters that enable command injection — always reject
_SHELL_DANGEROUS_CHARS = {";", "|", "&", "$", "`", "(", ")", "{", "}", "<", ">", "\n", "\r", "\0"}


def _validate_shell_command(command: str) -> None:
    """Validate a shell command for safety before execution.

    Raises InputValidationError or SecurityError on problems.
    """
    if not isinstance(command, str):
        raise InputValidationError("Command must be a string")
    stripped = command.strip()
    if not stripped:
        raise InputValidationError("Command cannot be empty")
    if len(stripped) > 2000:
        raise InputValidationError("Command too long (max 2000 characters)")
    # Reject shell metacharacters that enable injection
    bad = [c for c in stripped if c in _SHELL_DANGEROUS_CHARS]
    if bad:
        raise SecurityError(f"Command contains disallowed characters: {bad!r}")
    # Reject path traversal attempts
    if ".." in stripped:
        raise SecurityError("Command contains path traversal sequence '..'")


# Regex patterns for allowed shell commands (much safer than word lists)
ALLOWED_PATTERNS = {
    # python: run a .py file, optionally with arguments
    "python": r"^python3?\s+[\w./-]+\.py(\s+[\w./=-]+)*$",
    # node: run node scripts
    "node": r"^node\s+[\w./-]+\.js(\s+[\w./=-]+)*$",
    # git: safe read-only operations only
    "git": r"^git\s+(clone|pull|status|log|diff|show|ls-files|rev-parse)\s+[\w./:@#-]+$",
    # curl: fetch remote content (no file upload / POST data with secrets)
    # Removed -o (file write), -I (header leak), -O (remote name write) for security
    "curl": r"^curl\s+(-[sSL]|--silent|--show-error|--location)?\s*https?://[\w./:@\-_?=&]+(?:/[\w./:@\-_?=&-]+)*(\?[\w=&]+)?$",
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
        self._patterns = ALLOWED_PATTERNS

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = (ctx.config.get("execution") or {}).get("local_shell") or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._timeout = int(cfg.get("default_timeout", 60))
        self._workdir = cfg.get("workdir", self._workdir)
        # Merge config-level allowed_commands with built-in patterns
        allowed_commands = cfg.get("allowed_commands", [])
        if allowed_commands:
            self._patterns = dict(ALLOWED_PATTERNS)
            for cmd in allowed_commands:
                self._patterns[cmd] = rf"^{cmd}\s+[\w./-]+(\s+[\w./=-]+)*$"
        else:
            self._patterns = ALLOWED_PATTERNS
        Path(self._workdir).mkdir(parents=True, exist_ok=True)
        Path(self._audit_log_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "shell executor enabled=%s patterns=%d",
            self._enabled,
            len(self._patterns),
        )

    def can_run(self, command: str) -> bool:
        if not self._enabled:
            return False
        # Run structural validation first — reject injection attempts early
        try:
            _validate_shell_command(command)
        except (InputValidationError, SecurityError) as exc:
            logger.warning("shell command rejected: %s", exc)
            return False
        import re
        # Fix Bug #14: Remove duplicate backslash escape in character class
        # Original: [0-9a-zA-Z\s_./:@#\-='\"\\-]+ had \\- which is wrong
        # Fixed: [0-9a-zA-Z\s_./:@#='\"\-]+ - backslash at end, no need to escape dash
        if not re.fullmatch(r"[0-9a-zA-Z\s_./:@#='\"\-]+", command.strip()):
            return False
        for name, pattern in self._patterns.items():
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
        except Exception as exc:
            logger.warning("failed to write audit log: %s", exc)

    async def run(
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

        # parse safely without invoking a shell
        try:
            args = shlex.split(command, posix=True)
        except ValueError as exc:
            result = {"stdout": "", "stderr": f"[parse error: {exc}]", "returncode": -3, "blocked": False}
            if audit:
                self._audit(command, result)
            return result

        # Human-in-the-loop approval check for dangerous commands
        approval_manager = getattr(self.ctx, 'approval_manager', None) if self.ctx else None
        if approval_manager is not None:
            # Determine risk level based on command content
            if any(d in command for d in ["sudo", "rm -rf", "mkfs", "dd if="]):
                risk = "critical"
            elif any(d in command for d in ["rm", "delete", "drop", ">", "sudo", "chmod", "chown"]):
                risk = "high"
            else:
                risk = "medium"
            req = approval_manager.request_approval(
                operation=f"shell: {command[:100]}",
                details=command,
                risk_level=risk,
            )
            # Publish event so gateways (CLI/Web) can prompt the user
            self.publish("approval_needed", request=req.to_dict())
            approved = await req.wait(timeout=120.0)
            if not approved:
                result = {
                    "stdout": "",
                    "stderr": "[execution denied by user]",
                    "returncode": -1,
                    "blocked": True,
                }
                if audit:
                    self._audit(command, result)
                return result

        try:
            # Use asyncio.create_subprocess_exec to avoid blocking the event loop
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workdir,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout or self._timeout,
            )
            result = {
                "stdout": (stdout.decode() or "")[:8000],
                "stderr": (stderr.decode() or "")[:4000],
                "returncode": proc.returncode,
                "blocked": False,
            }
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()  # 等待进程终止，避免僵尸进程
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
        # Reuse the shell executor's allow-list — the same regex policy
        # applies to commands we run inside a hardened container.
        self._patterns = ALLOWED_PATTERNS

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

    def can_run(self, command: str) -> bool:
        """Validate command before executing in Docker container."""
        if not self._enabled:
            return False
        # Run structural validation — reject injection attempts early
        try:
            _validate_shell_command(command)
        except (InputValidationError, SecurityError) as exc:
            logger.warning("docker command rejected: %s", exc)
            return False
        import re
        # Basic safety: only allow printable ASCII with common shell-safe chars
        if not re.fullmatch(r"[0-9a-zA-Z\s_./:@#\-='\"\\-]+", command.strip()):
            return False
        for name, pattern in self._patterns.items():
            if re.fullmatch(pattern, command.strip()):
                return True
        return False

    async def run(self, command: str) -> Dict:
        if not self._enabled:
            return {
                "stdout": "", "stderr": "docker executor disabled",
                "returncode": -1, "blocked": False,
            }
        if not self.can_run(command):
            return {
                "stdout": "", "stderr": f"[security] command blocked: {command[:100]}",
                "returncode": -1, "blocked": True,
            }
        # Human-in-the-loop approval check for dangerous commands
        approval_manager = getattr(self.ctx, 'approval_manager', None) if self.ctx else None
        if approval_manager is not None:
            if any(d in command for d in ["sudo", "rm -rf", "mkfs", "dd if="]):
                risk = "critical"
            elif any(d in command for d in ["rm", "delete", "drop", ">", "sudo", "chmod", "chown"]):
                risk = "high"
            else:
                risk = "medium"
            req = approval_manager.request_approval(
                operation=f"docker: {command[:100]}",
                details=command,
                risk_level=risk,
            )
            self.publish("approval_needed", request=req.to_dict())
            approved = await req.wait(timeout=120.0)
            if not approved:
                return {
                    "stdout": "",
                    "stderr": "[execution denied by user]",
                    "returncode": -1,
                    "blocked": True,
                }
        # Use exec-form (argv) instead of shell-form to avoid container-internal
        # shell interpretation.  The container is already heavily sandboxed
        # (--network=none, --read-only, --cap-drop=all, non-root), but
        # exec-form is still preferred as defense-in-depth.
        try:
            cmd_parts = shlex.split(command, posix=True)
        except ValueError:
            return {
                "stdout": "", "stderr": "[parse error: cannot split command]",
                "returncode": -3, "blocked": True,
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
        ] + cmd_parts
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            return {
                "stdout": (stdout.decode() or "")[:8000],
                "stderr": (stderr.decode() or "")[:4000],
                "returncode": proc.returncode,
                "blocked": False,
            }
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()  # 等待进程终止，避免僵尸进程
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
                headers={"User-Agent": "One-Agent/2.0 (+https://github.com/huang2025511/one-agent)"},
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
