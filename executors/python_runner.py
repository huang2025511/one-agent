"""Python REPL executor — sandboxed code execution via isolated subprocess.

Allows the Agent to write and execute Python code to solve problems:
- Math calculations
- Data processing
- File manipulation
- Code generation and testing

Security architecture:
- **Subprocess isolation**: Code runs in a separate Python process, so
  even if the sandbox is escaped via ``().__class__.__bases__[0].__subclasses__()``
  the escape only affects the child process (which has no secrets, no
  network access to internal services, and is resource-limited).
- **Import whitelist**: Only safe stdlib modules are importable.
- **Resource limits**: Memory (RLIMIT_AS) and CPU (RLIMIT_CPU) are
  capped to prevent DoS.
- **Timeout enforcement**: The subprocess is killed (SIGKILL to the
  whole process group) on timeout — unlike threads, processes can be
  forcibly terminated.
- **Environment scrubbing**: API keys and other secrets are stripped
  from the child environment.

Previous design (exec-based sandbox with restricted builtins) was
insecure because Python attribute access cannot be blocked at the
language level — ``().__class__.__bases__[0].__subclasses__()`` reaches
``object`` without needing the ``object`` builtin name.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, Optional

from executors.base import BaseExecutor

logger = logging.getLogger(__name__)

# Python executor configuration
DEFAULT_EXECUTION_TIMEOUT = 10
DEFAULT_MAX_OUTPUT = 10_000
DEFAULT_MEMORY_LIMIT_MB = 256

# Whitelist of safe imports — no system/network access
_SAFE_IMPORTS = {
    "math",
    "random",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "operator",
    "string",
    "re",
    "json",
    "csv",
    "hashlib",
    "base64",
    "urllib.parse",
    "decimal",
    "fractions",
    "statistics",
    "typing",
    "dataclasses",
    "enum",
    "copy",
    "pprint",
    "textwrap",
    "unicodedata",
}

# Marker used to delimit the expression result in subprocess stdout.
# The sandbox wrapper writes ``__SANDBOX_RESULT__:<repr>`` as the last
# line when the code evaluates to a non-None expression.
_RESULT_MARKER = "__SANDBOX_RESULT__:"

# The sandbox wrapper script executed inside the subprocess.
# It is parameterized with the safe-imports set and resource limits.
# Using repr() of the set ensures safe embedding.
_SANDBOX_WRAPPER = '''
import sys, io, resource, builtins

# --- Resource limits (defense against memory/CPU DoS) ---
_mem_limit = {mem_limit}
_cpu_limit = {cpu_limit}
for _rlimit, _val in [
    (resource.RLIMIT_AS, (_mem_limit, _mem_limit)),
    (resource.RLIMIT_CPU, (_cpu_limit, _cpu_limit + 2)),
    (resource.RLIMIT_FSIZE, ({fsize_limit}, {fsize_limit})),
]:
    try:
        resource.setrlimit(_rlimit, _val)
    except (OSError, ValueError, AttributeError):
        pass

# --- Import whitelist ---
_real_import = builtins.__import__
_safe_imports = {safe_imports!r}
def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name not in _safe_imports and not any(name.startswith(s + ".") for s in _safe_imports):
        raise ImportError("Import not allowed: " + name)
    return _real_import(name, globals, locals, fromlist, level)
builtins.__import__ = _safe_import

# --- Read user code from stdin ---
_code = sys.stdin.buffer.read().decode("utf-8", "replace")

# --- Build restricted builtins ---
# Remove dangerous builtins that allow file I/O or code execution.
# The subprocess isolation (separate process + rlimits + env cleanup +
# import whitelist) is the primary defense against sandbox escapes;
# removing these builtins adds defense-in-depth against file exfiltration
# (e.g. open("config/default_config.yaml").read() to steal API keys).
# Introspection builtins (getattr, dir, vars, etc.) are kept because the
# subprocess has no dangerous modules loaded, making __subclasses__()
# escape chains non-exploitable.
_dangerous = {'open', 'exec', 'eval', 'compile', 'breakpoint',
              'exit', 'quit', '__import__'}
_safe_builtins = {k: v for k, v in builtins.__dict__.items()
                  if k not in _dangerous}

# --- Execute ---
_g = {{"__builtins__": _safe_builtins, "__name__": "__sandbox__"}}
try:
    try:
        _compiled = compile(_code, "<sandbox>", "eval")
        _r = eval(_compiled, _g)
        if _r is not None:
            sys.stdout.write("\\n{marker}" + repr(_r) + "\\n")
    except SyntaxError:
        _compiled = compile(_code, "<sandbox>", "exec")
        exec(_compiled, _g)
except SystemExit:
    raise
except Exception as _e:
    sys.stderr.write(type(_e).__name__ + ": " + str(_e) + "\\n")
    sys.exit(1)
'''


class PythonExecutor(BaseExecutor):
    """Sandboxed Python code executor using subprocess isolation."""

    name = "executor_python"
    depends_on = []

    def __init__(self) -> None:
        super().__init__()
        self._timeout = DEFAULT_EXECUTION_TIMEOUT
        self._max_output = DEFAULT_MAX_OUTPUT
        self._memory_limit_mb = DEFAULT_MEMORY_LIMIT_MB

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("execution", {}).get("python", {})
        self._timeout = cfg.get("timeout", DEFAULT_EXECUTION_TIMEOUT)
        self._max_output = cfg.get("max_output", DEFAULT_MAX_OUTPUT)
        self._memory_limit_mb = cfg.get("memory_limit_mb", DEFAULT_MEMORY_LIMIT_MB)

    def _build_sandbox_script(self, timeout: int) -> str:
        """Build the wrapper script to execute inside the subprocess."""
        return _SANDBOX_WRAPPER.format(
            safe_imports=_SAFE_IMPORTS,
            mem_limit=self._memory_limit_mb * 1024 * 1024,
            cpu_limit=timeout,
            fsize_limit=64 * 1024 * 1024,  # 64 MB max file size
            marker=_RESULT_MARKER,
        )

    @staticmethod
    def _build_sandbox_env() -> Dict[str, str]:
        """Build a scrubbed environment for the subprocess.

        Only essential, non-secret environment variables are passed.
        API keys, tokens, and other secrets are stripped to ensure
        that even a full sandbox escape cannot exfiltrate them.
        """
        safe_keys = {
            "PATH", "LANG", "LC_ALL", "LC_CTYPE",
            "HOME", "TMPDIR", "TMP", "TEMP",
            "SYSTEMROOT",  # Windows
        }
        env: Dict[str, str] = {}
        for k in safe_keys:
            v = os.environ.get(k)
            if v is not None:
                env[k] = v
        # Explicitly remove dangerous Python-related vars
        for dangerous in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
                          "PYTHONHTTPSVERIFY", "PYTHONBREAKPOINT"):
            env.pop(dangerous, None)
        return env

    async def execute(
        self,
        code: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute Python code in an isolated subprocess.

        Args:
            code: Python source code to execute
            timeout: Execution timeout in seconds (default: 10)

        Returns:
            {
                "success": bool,
                "output": str,  # stdout
                "error": str,   # stderr or exception
                "result": Any,  # value of last expression (if any)
                "duration_ms": float,
            }
        """
        if not code or not isinstance(code, str):
            raise ValueError("code must be a non-empty string")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ValueError("timeout must be a positive number")

        timeout = timeout or self._timeout
        start = time.time()
        sandbox_script = self._build_sandbox_script(timeout)
        env = self._build_sandbox_env()

        try:
            # start_new_session=True creates a new process group so
            # we can kill the whole tree (including any forked children)
            # on timeout.
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", sandbox_script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except Exception as exc:
            duration_ms = (time.time() - start) * 1000
            return {
                "success": False,
                "output": "",
                "error": f"Failed to start sandbox: {exc}",
                "result": None,
                "duration_ms": duration_ms,
            }

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=code.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Kill the entire process group (subprocess + any children)
            self._kill_process_group(proc)
            await proc.wait()
            duration_ms = (time.time() - start) * 1000
            return {
                "success": False,
                "output": "",
                "error": f"Execution timed out after {timeout}s",
                "result": None,
                "duration_ms": duration_ms,
            }
        except Exception as exc:
            self._kill_process_group(proc)
            await proc.wait()
            duration_ms = (time.time() - start) * 1000
            return {
                "success": False,
                "output": "",
                "error": str(exc),
                "result": None,
                "duration_ms": duration_ms,
            }

        duration_ms = (time.time() - start) * 1000
        output = stdout_bytes.decode("utf-8", errors="replace")
        error = stderr_bytes.decode("utf-8", errors="replace")
        success = proc.returncode == 0

        # Extract expression result if present (last line with marker)
        result: Any = None
        marker_idx = output.rfind(_RESULT_MARKER)
        if marker_idx != -1:
            result_line = output[marker_idx + len(_RESULT_MARKER):].strip()
            output = output[:marker_idx].rstrip()
            # Safely evaluate the repr() back to a Python object
            try:
                import ast
                result = ast.literal_eval(result_line)
            except (ValueError, SyntaxError):
                result = result_line

        return {
            "success": success,
            "output": output[: self._max_output],
            "error": error[: self._max_output],
            "result": result,
            "duration_ms": duration_ms,
        }

    @staticmethod
    def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
        """Kill the subprocess and its entire process group."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, AttributeError, OSError):
            # Fallback: kill just the process
            try:
                proc.kill()
            except (ProcessLookupError, RuntimeError):
                pass


# ------------------------------------------------------------------ skill handler


def make_python_handler(executor: PythonExecutor):
    """Create a skill handler for python_execute."""

    async def handler(args: Dict[str, Any]) -> str:
        code = args.get("code", "")
        if not code:
            return "请提供要执行的 Python 代码"

        timeout = args.get("timeout", 10)
        # Validate timeout type (LLM may pass string)
        if isinstance(timeout, str):
            try:
                timeout = int(timeout)
            except ValueError:
                timeout = 10
        result = await executor.execute(code, timeout=timeout)

        if result["success"]:
            parts = []
            if result["output"]:
                parts.append(f"输出:\n{result['output']}")
            if result["result"] is not None:
                parts.append(f"结果: {result['result']}")
            if not parts:
                parts.append("执行成功（无输出）")
            return "\n".join(parts)
        else:
            return f"执行失败:\n{result['error']}\n{result['output']}"

    return handler
