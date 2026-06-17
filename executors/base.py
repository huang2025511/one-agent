"""Base executor abstract class and unified result type.

Defined in a separate module to avoid circular imports between
``executors/__init__.py`` and executor subclasses (python_runner, system).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.plugin import Plugin


class ExecutorResult(dict):
    """Unified executor return type.

    All executors return an ExecutorResult (a dict subclass for backward
    compatibility) with these canonical fields:

    - success: bool        — whether the operation succeeded
    - stdout: str          — captured standard output
    - stderr: str          — captured standard error / error message
    - exit_code: int       — process exit code (0 = success, negative = signal/error)
    - error: Optional[str] — human-readable error description (None on success)
    - blocked: bool        — True if command was blocked by security policy
    - metadata: dict       — executor-specific extra data (risk_level, duration_ms, etc.)

    Legacy fields (returncode, ok, output) are provided as aliases for
    backward compatibility with existing callers.
    """

    @property
    def success(self) -> bool:
        return bool(self.get("success", self.exit_code == 0 and not self.get("blocked", False)))

    @property
    def stdout(self) -> str:
        return self.get("stdout", "")

    @property
    def stderr(self) -> str:
        return self.get("stderr", "")

    @property
    def exit_code(self) -> int:
        return int(self.get("exit_code", self.get("returncode", 0)))

    @property
    def error(self) -> Optional[str]:
        return self.get("error")

    @property
    def blocked(self) -> bool:
        return bool(self.get("blocked", False))

    # Legacy aliases (backward compatibility)
    @property
    def returncode(self) -> int:
        return self.exit_code

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def output(self) -> str:
        return self.stdout


def _to_executor_result(raw: Dict[str, Any]) -> ExecutorResult:
    """Normalize a legacy executor return dict into ExecutorResult.

    Maps legacy field names to canonical ones while preserving both.
    """
    result = dict(raw)
    # Map legacy fields to canonical if canonical is missing
    if "exit_code" not in result and "returncode" in result:
        result["exit_code"] = result["returncode"]
    if "exit_code" not in result and "ok" in result:
        result["exit_code"] = 0 if result["ok"] else 1
    if "success" not in result:
        rc = result.get("exit_code", result.get("returncode", 0))
        blocked = result.get("blocked", False)
        result["success"] = (rc == 0) and not blocked
    if "stdout" not in result and "output" in result:
        result["stdout"] = result["output"]
    if "error" not in result:
        err = result.get("stderr", "")
        if err and not result.get("success", True):
            result["error"] = err
    if "metadata" not in result:
        result["metadata"] = {}
    return ExecutorResult(result)


class BaseExecutor(Plugin):
    """Abstract base class for all executors.

    Subclasses should implement ``execute()`` returning an ExecutorResult.
    Legacy methods (run/fetch/dispatch) are preserved as aliases that
    delegate to execute() or vice versa.
    """

    name = "executor_base"

    async def execute(self, *args, **kwargs) -> ExecutorResult:
        """Unified entry point — subclasses must override."""
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement execute()"
        )
