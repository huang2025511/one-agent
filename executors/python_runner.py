"""Python REPL executor — sandboxed code execution for the Agent.

Allows the Agent to write and execute Python code to solve problems:
- Math calculations
- Data processing
- File manipulation
- Code generation and testing

Security:
- Restricted builtins (no os.system, subprocess, etc.)
- Whitelist of safe imports
- Timeout enforcement
- Output capture (stdout/stderr)
"""

from __future__ import annotations

import asyncio
import builtins
import io
import logging
import sys
import time
from contextlib import redirect_stdout, redirect_stderr
from typing import Any, Dict, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)

# Save reference to the real __import__ before we override it
_real_import = builtins.__import__

# Whitelist of safe imports — no system/network access
_SAFE_IMPORTS = {
    "math",
    "random",
    "datetime",
    # "time",  # Removed: time.sleep() can be used for DoS attacks
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

# Safe import wrapper that checks against whitelist
def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Safe import function that only allows whitelisted modules."""
    # Check if module or its parent is in whitelist
    if name not in _SAFE_IMPORTS and not any(name.startswith(s + ".") for s in _SAFE_IMPORTS):
        raise ImportError(f"Import not allowed: {name}")
    return _real_import(name, globals, locals, fromlist, level)

# Safe builtins — exclude dangerous ones like eval, exec, compile
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bin": bin,
    "bool": bool,
    "bytes": bytes,
    "callable": callable,
    "chr": chr,
    "complex": complex,
    "dict": dict,
    "dir": dir,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "format": format,
    "frozenset": frozenset,
    "getattr": getattr,
    "hasattr": hasattr,
    "hash": hash,
    "hex": hex,
    "id": id,
    "int": int,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "object": object,
    "oct": oct,
    "ord": ord,
    "pow": pow,
    "print": print,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    "True": True,
    "False": False,
    "None": None,
    "__import__": _safe_import,
}


class PythonExecutor(Plugin):
    """Sandboxed Python code executor for the Agent."""

    name = "executor_python"
    depends_on = []

    def __init__(self) -> None:
        super().__init__()
        self._timeout = 10  # seconds
        self._max_output = 10_000  # chars

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("execution", {}).get("python", {})
        self._timeout = cfg.get("timeout", 10)
        self._max_output = cfg.get("max_output", 10_000)

    async def execute(
        self,
        code: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute Python code in a sandboxed environment.

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
        timeout = timeout or self._timeout
        start = time.time()

        # Capture stdout/stderr
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        # Build safe globals
        safe_globals = {"__builtins__": _SAFE_BUILTINS}

        # Execute in a thread pool to enforce timeout
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    self._run_code,
                    code,
                    safe_globals,
                    stdout_capture,
                    stderr_capture,
                ),
                timeout=timeout,
            )
            duration_ms = (time.time() - start) * 1000

            output = stdout_capture.getvalue()[: self._max_output]
            error = stderr_capture.getvalue()[: self._max_output]

            return {
                "success": result.get("success", False),
                "output": output,
                "error": error or result.get("error", ""),
                "result": result.get("result"),
                "duration_ms": duration_ms,
            }

        except asyncio.TimeoutError:
            duration_ms = (time.time() - start) * 1000
            return {
                "success": False,
                "output": stdout_capture.getvalue()[: self._max_output],
                "error": f"Execution timed out after {timeout}s",
                "result": None,
                "duration_ms": duration_ms,
            }
        except Exception as exc:
            duration_ms = (time.time() - start) * 1000
            return {
                "success": False,
                "output": "",
                "error": str(exc),
                "result": None,
                "duration_ms": duration_ms,
            }

    @staticmethod
    def _run_code(
        code: str,
        safe_globals: Dict[str, Any],
        stdout_capture: io.StringIO,
        stderr_capture: io.StringIO,
    ) -> Dict[str, Any]:
        """Run code in a separate thread (called by run_in_executor)."""
        result = {"success": False, "result": None, "error": ""}

        try:
            # Check for unsafe imports
            for line in code.split("\n"):
                line = line.strip()
                if line.startswith("import ") or line.startswith("from "):
                    # Extract full module path (e.g., "urllib.parse" from "from urllib.parse import quote")
                    parts = line.split()
                    if len(parts) >= 2:
                        module = parts[1].split(".")[0] if parts[0] == "import" else parts[1]
                        # Check if the full module path or its root is in whitelist
                        if module not in _SAFE_IMPORTS and not any(module.startswith(s + ".") for s in _SAFE_IMPORTS):
                            result["error"] = f"Import not allowed: {module}"
                            return result

            # Compile and execute
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                # Try to evaluate as expression first (for simple calculations)
                try:
                    compiled = compile(code, "<string>", "eval")
                    result["result"] = eval(compiled, safe_globals)
                    result["success"] = True
                    return result
                except SyntaxError:
                    # Not a simple expression — execute as statements
                    pass

                # Execute as statements
                compiled = compile(code, "<string>", "exec")
                exec(compiled, safe_globals)
                result["success"] = True

        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"

        return result


# ------------------------------------------------------------------ skill handler


def make_python_handler(executor: PythonExecutor):
    """Create a skill handler for python_execute."""

    async def handler(args: Dict[str, Any]) -> str:
        code = args.get("code", "")
        if not code:
            return "请提供要执行的 Python 代码"

        timeout = args.get("timeout", 10)
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
