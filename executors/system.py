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
import shlex
import signal
import subprocess
import time
from typing import Any, Dict, Optional, Tuple

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

def classify_command(command: str) -> Tuple[int, str]:
    """Classify a command into risk level and return (level, reason).

    Uses the unified IntentClassifier from utils.intent_classifier
    instead of inline keyword matching.
    """
    from utils.intent_classifier import get_classifier

    classifier = get_classifier()
    return classifier.classify_command(command)


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
        """Return True if a password is set (plaintext or hash)."""
        return bool(self._password_hash)

    def verify(self, password: str) -> bool:
        """Check plaintext password against stored value.

        支持三种存储格式（均向后兼容）：
        - ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`` (推荐，带盐)
        - 64 字符 hex SHA-256 (legacy，无盐)
        - 明文密码 (用户直接在 yaml 中填写明文，最简单)

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
        # 64 字符 hex → legacy SHA-256 校验
        if len(stored) == 64 and all(c in '0123456789abcdefABCDEF' for c in stored):
            trial = hashlib.sha256(password.encode()).hexdigest()
            return hmac.compare_digest(trial, stored)
        # 否则视为明文密码直接比较（方便用户在 yaml 中直接填明文）
        return hmac.compare_digest(stored, password)

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
        # 修复 #2（安全）：读取 require_password_for_dangerous 配置。
        # 之前 _check_permission 无条件放行所有命令（含 DANGEROUS），
        # 导致 PasswordManager 整套 PBKDF2+lockout 机制成为死代码，
        # 配置和 UI 的开关成为"安全剧场"。现在恢复分级判断。
        self._require_password = bool(sec_cfg.get("require_password_for_dangerous", True))
        self._max_output_bytes = int(sec_cfg.get("max_output_bytes", 100_000))
        self._timeout_seconds = int(sec_cfg.get("command_timeout_seconds", 30))
        self._workdir = str(sec_cfg.get("command_workdir", "."))

        if not self._pwd_manager.is_configured():
            logger.info(
                "system_executor: no password configured. "
                "DANGEROUS commands will be %s. "
                "如需启用密码保护，请在 config 中设置 security.system_executor_password",
                "BLOCKED (require_password_for_dangerous=true)" if self._require_password
                else "ALLOWED (require_password_for_dangerous=false)",
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

        修复 #2（安全）：恢复分级密码保护逻辑。

        分级策略：
        - risk_level 0-1 (SAFE/LOW)：直接放行，无需密码
        - risk_level 2-3 (MEDIUM/DANGEROUS)：
          - 若 require_password_for_dangerous=false：放行（用户明确关闭保护）
          - 若 require_password_for_dangerous=true：
            - 未配置密码：拒绝，提示用户配置密码
            - 已配置密码：检查缓存 → 验证密码 → 锁定机制
        """
        # 低风险命令直接放行
        if risk_level < 2:
            return (True, False)

        # 高风险命令，但用户明确关闭了密码保护
        if not self._require_password:
            return (True, False)

        # 高风险命令，需要密码保护
        pwd_mgr = self._pwd_manager
        if pwd_mgr is None:
            # PasswordManager 未初始化（setup 未跑或 executor disabled）
            return (False, True)

        # 未配置密码 → 拒绝（而非放行，避免安全剧场）
        if not pwd_mgr.is_configured():
            logger.warning(
                "system_executor: DANGEROUS command blocked — "
                "require_password_for_dangerous=true but no password configured. "
                "请在 config 中设置 security.system_executor_password"
            )
            return (False, True)

        # 检查密码缓存（避免短时间内反复要求输入密码）
        if pwd_mgr.is_cached(risk_level):
            return (True, False)

        # 检查锁定状态
        if not pwd_mgr.can_attempt():
            return (False, True)

        # 验证提供的密码
        if not provided_password:
            # 未提供密码，需要用户输入
            return (False, True)

        if pwd_mgr.verify(provided_password):
            pwd_mgr.record_success(risk_level)
            return (True, False)

        # 密码错误
        pwd_mgr.record_failure()
        logger.warning(
            "system_executor: password verification failed (attempt %d/%d)",
            pwd_mgr._failed_count, pwd_mgr._max_attempts,
        )
        return (False, True)

    # ------------------------------------------------------------ execution
    async def _execute(
        self, command: str, workdir: str, risk_level: int,
    ) -> Dict[str, Any]:
        """Execute the command via subprocess.

        Uses ``run_subprocess_async`` (统一收口到 core/subprocess_utils.py)
        with ``shlex.split`` 而非 shell，避免 shell 元字符注入。
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

            # 统一收口到 run_subprocess_async：内部用 asyncio.to_thread +
            # subprocess.run，不阻塞事件循环。preexec_fn=os.setsid 让子进程
            # 独立成进程组；超时时 subprocess.run 会 SIGKILL 直接子进程
            # （注意：与原 create_subprocess_exec + os.killpg 相比，不再
            # 杀整个进程组，但常见单命令场景不受影响）。
            from core.subprocess_utils import run_subprocess_async
            try:
                completed = await run_subprocess_async(
                    argv,
                    timeout=self._timeout_seconds,
                    cwd=workdir or ".",
                    capture_output=True,
                    text=True,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "error": f"command timed out after {self._timeout_seconds}s",
                    "risk_level": risk_level,
                    "risk_label": self.RISK_LABELS.get(risk_level, "UNKNOWN"),
                }

            out = (completed.stdout or "")[:self._max_output_bytes]
            err = (completed.stderr or "")[:self._max_output_bytes]

            return {
                "ok": completed.returncode == 0,
                "stdout": out,
                "stderr": err,
                "exit_code": completed.returncode,
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
