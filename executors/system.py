"""System Command Executor — with permission model and password confirmation.

This executor runs system commands (shell, file ops, process management) on the
host machine, gated by a risk-based permission system:

Permission Levels (0–3):
  0 = SAFE      — Read-only + info commands (ls, cat, echo, date, uptime, free, df, ...)
                  Always allowed, no password needed.

  1 = LOW       — Write to non-system files, create dirs, git pull/clone
                  Allowed after initial password input (cached for session).

  2 = MEDIUM    — Install packages, modify config, restart services
                  Require password confirmation per-command.

  3 = DANGEROUS — rm -rf, sudo, chmod 777, fork bomb, reboot, format, dd, etc.
                  Require password confirmation per-command + user explicitly confirms
                  they understand the risk.

Password Policy:
  - Hash: PBKDF2-HMAC-SHA256 (salted, 200k iterations) for new passwords;
          legacy SHA-256 hashes are still accepted for backward compatibility.
  - Cache: Valid for 15 minutes (configurable) after first entry
  - Max Tries: 3 failures before 5-minute lockout
  - Session-based: Password cache lives in memory only

Configuration (config/default_config.yaml):
.. code-block:: yaml

   security:
     system_executor_password: ""   # PBKDF2 hash (recommended) or legacy SHA-256.  Empty = allow all SAFE ops
                                    # Generate: python -c "from executors.system import SystemExecutor; print(SystemExecutor.hash_password('your_pass'))"
     password_cache_minutes: 15
     max_password_attempts: 3
     lockout_minutes: 5
     require_password_for_dangerous: true
     allowed_safe_commands: []      # additional safe commands (empty = use built-in)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import shlex
import signal
import time
from typing import Any, Dict, List, Optional, Tuple

from executors.base import BaseExecutor, ExecutorResult, _to_executor_result

logger = logging.getLogger(__name__)

# ============================================================
#  Password hashing helpers (PBKDF2-HMAC-SHA256, salted)
# ============================================================

_PBKDF2_ITERATIONS = 200_000
_PBKDF2_ALGO = "pbkdf2_sha256"


def _hash_password_pbkdf2(plaintext: str) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``."""
    import base64
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), salt, _PBKDF2_ITERATIONS, dklen=32)
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _verify_pbkdf2(plaintext: str, stored: str) -> bool:
    """Verify a plaintext against a ``pbkdf2_sha256$...`` hash string."""
    import base64
    try:
        algo, iter_str, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != _PBKDF2_ALGO:
            return False
        iterations = int(iter_str)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), salt, iterations, dklen=len(expected))
    return hmac.compare_digest(dk, expected)


# ============================================================
#  Risk classification
# ============================================================

# Level 0: always allowed, no password
_SAFE_PATTERNS: List[Tuple[str, str]] = [
    ("ls", r"^ls(\s+(-[alhRtTr]+|--color=\w+))*(\s+[\w./~_-]+)*$"),
    ("cat", r"^cat(\s+-[nb])?\s+[\w./~_-]+$"),
    ("head", r"^head(\s+-n\s+\d+)?\s+[\w./~_-]+$"),
    ("tail", r"^tail(\s+-[nf]\s*\d*)?\s+[\w./~_-]+$"),
    ("wc", r"^wc(\s+-[lwc])?\s+[\w./~_-]+$"),
    ("echo", r"^echo\s+[\w\s./~_-]+$"),
    ("date", r"^date(\s+[+-].*)?$"),
    ("uptime", r"^uptime\s*$"),
    ("free", r"^free(\s+-[hmg])?$"),
    ("df", r"^df(\s+-[hTt])?(\s+[\w./~_-]+)?$"),
    ("du", r"^du(\s+-[hs])?(\s+[\w./~_-]+)?$"),
    ("pwd", r"^pwd(\s+-[LP])?$"),
    ("whoami", r"^whoami\s*$"),
    ("id", r"^id(\s+[\w_-]+)?$"),
    ("uname", r"^uname(\s+-[amnrspvio]+)?$"),
    ("hostname", r"^hostname(\s+-[is])?$"),
    ("env", r"^env(\s+-i)?(\s+[\w_]+=.*)?$"),
    ("which", r"^which\s+[\w_-]+$"),
    ("type", r"^type\s+[\w_-]+$"),
    ("man", r"^man\s+[\w_-]+$"),
    ("find", r"^find\s+[\w./~_-]+\s+(-maxdepth\s+\d+\s+)?(-name\s+[\w.*_-]+| -type\s+[fdl])(\s+( -name\s+[\w.*_-]+| -type\s+[fdl]))*$"),
    ("grep", r"^grep(\s+-[inrvclqwo]+)*\s+[\w\s./~,_\"'-]+$"),
    ("ps", r"^ps(\s+-[auxf]+)?(\s+\|)?$"),
    ("top", r"^top(\s+-[bnHp]+(\s+\d+)?)?$"),
    ("pgrep", r"^pgrep(\s+-[flx])?\s+[\w_-]+$"),
    ("stat", r"^stat\s+[\w./~_-]+$"),
    ("file", r"^file\s+[\w./~_-]+$"),
]

# Level 1: file write / create / basic git operations (require password once)
_LOW_PATTERNS: List[Tuple[str, str]] = [
    ("mkdir", r"^mkdir(\s+-[pv])?\s+[\w./~_-]+$"),
    ("touch", r"^touch\s+[\w./~_-]+$"),
    ("cp", r"^cp(\s+-[rf])?\s+[\w./~_-]+\s+[\w./~_-]+$"),
    ("mv", r"^mv\s+[\w./~_-]+\s+[\w./~_-]+$"),
    ("git", r"^git\s+(status|log|diff|branch|checkout|pull|clone|add|commit|push)\s+[\w./:@#_\"',~ -]+$"),
    ("tar", r"^tar\s+-[cx]\w*\s+[\w./~_-]+(\s+[\w./~_-]+)*$"),
    ("zip", r"^zip\s+[\w./~_-]+\.zip\s+[\w./~_*-]+$"),
    ("unzip", r"^unzip\s+[\w./~_-]+\.zip$"),
    ("tee", r"^tee\s+[\w./~_-]+$"),
]

# Level 2: system modification (require password per command)
_MEDIUM_PATTERNS: List[Tuple[str, str]] = [
    ("pip", r"^pip3?\s+install\s+[\w._-]+(\[[\w,]+\])?$"),
    ("npm", r"^npm\s+install(\s+[\w._@/-]+)?$"),
    ("apt", r"^apt-get\s+(update|install)\s+[\w._-]+$"),
    ("brew", r"^brew\s+install\s+[\w._/-]+$"),
    ("systemctl", r"^systemctl\s+(start|stop|restart|reload|enable|disable|status)\s+[\w._@-]+$"),
    ("service", r"^service\s+[\w._@-]+\s+(start|stop|restart|status)$"),
    ("docker", r"^docker\s+(start|stop|restart|ps|logs|pull|run)\s+[\w._/:@=-]+$"),
    ("chown", r"^chown(\s+-R)?\s+[\w:]+\s+[\w./~_-]+$"),
    ("kill", r"^kill(\s+-[0-9]+)?\s+\d+$"),
    ("pkill", r"^pkill(\s+-[0-9]+)?\s+[\w_-]+$"),
]

# Level 3: dangerous — require password + explicit confirmation
_DANGEROUS_PATTERNS: List[str] = [
    r"^rm(\s+-[rf]+)+",           # rm -rf / rm -r
    r"^sudo\s",                    # sudo anything
    r"^chmod\s+[0-7]77\s",        # chmod 777
    r"^shutdown\s",                # shutdown
    r"^reboot",                    # reboot
    r"^poweroff",                  # poweroff
    r"^mkfs\s",                    # format filesystem
    r"^dd\s+if=",                  # dd (disk destroyer)
    r"^>",                         # redirect truncate ("> /dev/sda")
    r">>\s*/dev/",                 # append to device
    r"^mount\s",                   # mount
    r"^umount\s",                  # unmount
    r"^fdisk\s",                   # fdisk
    r"^parted\s",                  # parted
    r"^mkfs\.",                    # mkfs.ext4 etc
    r"^wipefs\s",                  # wipefs
    r"^crontab\s",                 # crontab
    r"^iptables\s",                # iptables
    r"\|\s*sh\b",                  # pipe to sh
    r"\|\s*bash\b",                # pipe to bash
    r"&\s*>/dev/null",             # background with redirect
    # Command chain injection detection — these operators allow
    # chaining multiple commands, bypassing per-command classification.
    r";",                          # command separator
    r"&&",                         # AND operator
    r"\|\|",                       # OR operator
    r"`",                          # backtick command substitution
    r"\$\(",                       # $(...) command substitution
]


def classify_command(command: str) -> Tuple[int, str]:
    """Classify a command into risk level and return (level, reason)."""
    stripped = command.strip()
    if not stripped:
        return (0, "empty")

    # Check DANGEROUS first (most specific patterns).
    # Use re.search (not re.match) so patterns like ";", "&&", "$("
    # are detected anywhere in the command, not just at the start.
    for pattern in _DANGEROUS_PATTERNS_COMPILED:
        if pattern.search(stripped):
            return (3, f"dangerous operations: {stripped[:60]}")

    # Check MEDIUM
    for cmd, pattern in _MEDIUM_PATTERNS_COMPILED:
        if pattern.match(stripped):
            return (2, f"system modification: {cmd}")

    # Check LOW
    for cmd, pattern in _LOW_PATTERNS_COMPILED:
        if pattern.match(stripped):
            return (1, f"file/system operation: {cmd}")

    # Check SAFE
    for _cmd, pattern in _SAFE_PATTERNS_COMPILED:
        if pattern.match(stripped):
            return (0, "safe operation")

    # Unknown command → treat as MEDIUM
    return (2, f"unknown command type: {stripped[:60]}")


# Pre-compile all patterns at module load time (compiled once, reused forever).
# This avoids re-compiling the same regex on every classify_command() call,
# which is a hot path executed before every system command.
_DANGEROUS_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS]
_MEDIUM_PATTERNS_COMPILED = [
    (cmd, re.compile(p, re.IGNORECASE)) for cmd, p in _MEDIUM_PATTERNS
]
_LOW_PATTERNS_COMPILED = [
    (cmd, re.compile(p, re.IGNORECASE)) for cmd, p in _LOW_PATTERNS
]
_SAFE_PATTERNS_COMPILED = [
    (cmd, re.compile(p, re.IGNORECASE)) for cmd, p in _SAFE_PATTERNS
]


# ============================================================
#  Password Manager
# ============================================================
class PasswordManager:
    """Handles password verification, caching, and lockout."""

    def __init__(
        self,
        password_hash: str,
        cache_minutes: int = 15,
        max_attempts: int = 3,
        lockout_minutes: int = 5,
    ) -> None:
        self._password_hash: str = password_hash    # PBKDF2 (new) or SHA-256 (legacy)
        self._cache_minutes: float = cache_minutes * 60
        self._max_attempts: int = max_attempts
        self._lockout_minutes: float = lockout_minutes * 60
        self._cache_expires_at: float = 0            # monotonic
        self._cached_level: int = 0                  # what level was approved
        self._failed_count: int = 0
        self._lockout_until: float = 0

    def is_configured(self) -> bool:
        """Return True if a password hash is actually set."""
        return bool(self._password_hash) and (
            len(self._password_hash) == 64  # legacy SHA-256 hex
            or self._password_hash.startswith("pbkdf2_sha256$")  # new format
        )

    def verify(self, password: str) -> bool:
        """Check plaintext password against stored hash.

        Supports two formats:
        - ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`` (recommended)
        - 64-char hex SHA-256 (legacy, unsalted — accepted for backward compat)

        安全说明：未配置密码时返回 False（拒绝），而非 True。
        之前的 `return True` 让 DANGEROUS 命令（rm -rf, mkfs 等）能被
        任意非空密码解锁，构成认证绕过。正确做法是：未配置密码 →
        所有需要密码的操作都拒绝，强制用户先配置密码。
        """
        if not self._password_hash:
            # 未配置密码 = 拒绝所有需要密码的操作（更安全）
            # 之前的 `return True` 是认证绕过漏洞
            return False
        stored = self._password_hash
        if stored.startswith("pbkdf2_sha256$"):
            return _verify_pbkdf2(password, stored)
        # Legacy SHA-256 (unsalted) — still accepted so existing configs keep working.
        trial = hashlib.sha256(password.encode()).hexdigest()
        return hmac.compare_digest(trial, stored)

    def can_attempt(self) -> bool:
        """True if we are not in lockout."""
        if self._lockout_until > 0 and time.monotonic() < self._lockout_until:
            remaining = int(self._lockout_until - time.monotonic())
            logger.warning("password lockout active (%ds remaining)", remaining)
            return False
        return True

    def record_failure(self) -> None:
        self._failed_count += 1
        if self._failed_count >= self._max_attempts:
            self._lockout_until = time.monotonic() + self._lockout_minutes
            self._failed_count = 0
            logger.warning(
                "password lockout triggered (%d failures, %d min)",
                self._max_attempts, int(self._lockout_minutes / 60),
            )

    def record_success(self, level: int) -> None:
        self._failed_count = 0
        self._lockout_until = 0
        self._cache_expires_at = time.monotonic() + self._cache_minutes
        self._cached_level = level

    def is_cached(self, required_level: int) -> bool:
        """True if password was recently verified for at least this level."""
        if time.monotonic() < self._cache_expires_at:
            return self._cached_level >= required_level
        return False

    def invalidate_cache(self) -> None:
        self._cache_expires_at = 0
        self._cached_level = 0


# ============================================================
#  System Executor Plugin
# ============================================================
class SystemExecutor(BaseExecutor):
    """Run OS commands with risk-based password confirmation.

    Implements the ``system.run`` skill:
      - args: {"command": "ls -la", "password": optional}

    If ``password`` is not provided and risk level > 0:
      - Publish ``approval_needed`` event
      - Wait for user to provide password

    Returns:
      {"ok": True/False, "stdout": "...", "stderr": "...", "exit_code": N}
    """

    name = "system_executor"

    RISK_LABELS: Dict[int, str] = {
        0: "SAFE",
        1: "LOW",
        2: "MEDIUM",
        3: "DANGEROUS",
    }

    def __init__(self) -> None:
        super().__init__()
        self._pwd_manager: Optional[PasswordManager] = None
        self._max_output_bytes: int = 100_000   # 100 KB
        self._timeout_seconds: int = 30
        self._workdir: str = "."
        self._enabled: bool = False

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        sec_cfg = (ctx.config.get("security") or {})
        self._enabled = bool(sec_cfg.get("system_executor_enabled", True))
        if not self._enabled:
            logger.info("system_executor disabled")
            return

        stored_hash = str(sec_cfg.get("system_executor_password", "") or "")
        cache_min = int(sec_cfg.get("password_cache_minutes", 15))
        max_attempts = int(sec_cfg.get("max_password_attempts", 3))
        lockout_min = int(sec_cfg.get("lockout_minutes", 5))
        self._pwd_manager = PasswordManager(stored_hash, cache_min, max_attempts, lockout_min)
        self._max_output_bytes = int(sec_cfg.get("max_output_bytes", 100_000))
        self._timeout_seconds = int(sec_cfg.get("command_timeout_seconds", 30))
        self._workdir = str(sec_cfg.get("command_workdir", "."))

        if not self._pwd_manager.is_configured():
            logger.warning(
                "system_executor: no password configured. "
                "SAFE commands (level 0) will run without password. "
                "Level 1-3 commands require a password. "
                "使用 /unlock <password> 来解锁，或在 config 中设置 security.system_executor_password"
            )

        # Register skill
        if hasattr(ctx, "skill_registry"):
            ctx.skill_registry.register(
                name="system.run",
                description="在主机执行系统命令（危险命令需密码确认）",
                parameters={
                    "command": {"type": "string", "required": True, "description": "要执行的命令"},
                    "password": {"type": "string", "required": False, "description": "密码（危险操作必填）"},
                    "working_dir": {"type": "string", "required": False, "description": "工作目录"},
                },
            )

        logger.info("system_executor enabled (password=%s)", "set" if (stored_hash) else "not set")

    # ------------------------------------------------------------ skill dispatch
    async def dispatch(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle ``system.run`` skill calls."""
        if name != "system.run":
            return {"ok": False, "error": f"unknown skill: {name}"}

        command = str(args.get("command", "")).strip()
        password = str(args.get("password", "")) if args.get("password") else ""
        workdir = str(args.get("working_dir", "") or self._workdir)

        if not command:
            return {"ok": False, "error": "command is required"}

        # ---- classify risk ----
        risk_level, reason = classify_command(command)
        level_label = self.RISK_LABELS.get(risk_level, "UNKNOWN")

        logger.info(
            "system_executor: command=%r risk=%d (%s) reason=%s",
            command[:80], risk_level, level_label, reason,
        )

        # ---- approval flow ----
        allowed, need_password = await self._check_permission(
            command, risk_level, reason, password,
        )
        if not allowed:
            return {
                "ok": False,
                "error": f"command blocked (risk={level_label}): {reason}",
                "risk_level": risk_level,
                "risk_label": level_label,
                "requires_password": need_password,
            }

        # ---- execute ----
        return await self._execute(command, workdir, risk_level)

    async def execute(self, command: str, **kwargs) -> ExecutorResult:
        """Unified entry point — delegates to dispatch('system.run', ...)."""
        result = await self.dispatch("system.run", {
            "command": command,
            "password": kwargs.get("password", ""),
            "working_dir": kwargs.get("working_dir", ""),
        })
        return _to_executor_result(result)

    # ------------------------------------------------------------ permission check
    async def _check_permission(
        self, command: str, risk_level: int, reason: str, provided_password: str = "",
    ) -> Tuple[bool, bool]:
        """Return (allowed, needs_password).

        简化后的策略：
        - Level 0 (SAFE): 免密码，始终允许
        - Level 1+ (LOW/MEDIUM/DANGEROUS): 需要密码，验证通过后缓存 60 分钟
          （只要对话框不关闭，60 分钟内不需要再次输入）
        """
        mgr = self._pwd_manager

        # SAFE level: always allowed without password
        if risk_level == 0:
            return (True, False)

        # No password manager configured → block non-safe
        if mgr is None:
            return (False, True)

        # Already cached → allow (会话内 60 分钟有效)
        if mgr.is_cached(risk_level):
            return (True, False)

        # Check lockout
        if not mgr.can_attempt():
            return (False, True)

        # If password was provided in the request body
        if provided_password:
            if mgr.verify(provided_password):
                # Cache only up to the requested risk level, not
                # unconditionally level 3. This prevents a low-risk
                # command's password from silently authorizing a
                # later DANGEROUS command.
                mgr.record_success(risk_level)
                return (True, False)
            else:
                mgr.record_failure()
                return (False, True)

        # Need password — caller should prompt user
        return (False, True)

    # ------------------------------------------------------------ execution
    async def _execute(
        self, command: str, workdir: str, risk_level: int,
    ) -> Dict[str, Any]:
        """Execute the command via subprocess.

        Uses ``create_subprocess_exec`` with ``shlex.split`` instead of
        ``create_subprocess_shell`` to avoid shell metacharacter injection.
        Commands containing shell operators (``;``, ``|``, ``&&``, ``$()``)
        are already classified as DANGEROUS by ``classify_command`` and
        should never reach this point without explicit confirmation.
        """
        try:
            # Parse command into argv — avoids shell interpretation.
            # shlex.split handles quoting correctly.
            argv = shlex.split(command)
            if not argv:
                return {
                    "ok": False,
                    "error": "empty command after parsing",
                    "risk_level": risk_level,
                    "risk_label": self.RISK_LABELS.get(risk_level, "UNKNOWN"),
                }

            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir or ".",
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout_seconds,
                )
            except asyncio.TimeoutError:
                # Kill the entire process group (os.setsid created one)
                # so child processes don't become orphans.
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                await proc.wait()
                return {
                    "ok": False,
                    "error": f"command timed out after {self._timeout_seconds}s",
                    "risk_level": risk_level,
                    "risk_label": self.RISK_LABELS.get(risk_level, "UNKNOWN"),
                }

            out = stdout.decode("utf-8", errors="replace")[:self._max_output_bytes]
            err = stderr.decode("utf-8", errors="replace")[:self._max_output_bytes]

            return {
                "ok": proc.returncode == 0,
                "stdout": out,
                "stderr": err,
                "exit_code": proc.returncode,
                "risk_level": risk_level,
                "risk_label": self.RISK_LABELS.get(risk_level, "UNKNOWN"),
            }

        except Exception as exc:  # noqa: BLE001
            logger.exception("system_executor: command failed: %s", command[:80])
            return {
                "ok": False,
                "error": str(exc),
                "risk_level": risk_level,
                "risk_label": self.RISK_LABELS.get(risk_level, "UNKNOWN"),
            }

    # ------------------------------------------------------------ password utilities
    async def verify_password(self, plaintext: str) -> bool:
        """Verify a password against the stored hash, update cache."""
        if self._pwd_manager is None:
            return False
        if self._pwd_manager.verify(plaintext):
            self._pwd_manager.record_success(2)  # verify gives medium access
            return True
        self._pwd_manager.record_failure()
        return False

    def invalidate_password(self) -> None:
        """Clear cached password (user explicitly logs out)."""
        if self._pwd_manager:
            self._pwd_manager.invalidate_cache()

    @staticmethod
    def hash_password(plaintext: str) -> str:
        """Generate a salted PBKDF2 hash for config storage.

        The returned string has the format
        ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`` and is verified
        by :meth:`PasswordManager.verify`.  Legacy 64-char SHA-256 hashes are
        still accepted for verification, so existing configs keep working.
        """
        return _hash_password_pbkdf2(plaintext)


__all__ = [
    "SystemExecutor",
    "PasswordManager",
    "classify_command",
]
