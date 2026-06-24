"""Async-friendly subprocess helpers.

Consolidates the ``asyncio.to_thread(subprocess.run, ...)`` wrappers
that were duplicated in ``skills/updater.py`` (``_run_git_async``,
``_run_subprocess_async``) and needed by ``multimodal/__init__.py``
(where ``subprocess.run`` was called directly inside ``async`` handlers,
blocking the event loop).

Calling ``subprocess.run`` directly from an ``async`` function freezes
the entire event loop for the duration of the subprocess — every
pending coroutine, timer, and network socket stalls until the child
process exits. Wrapping the blocking call in :func:`asyncio.to_thread`
lets the event loop continue servicing other tasks while the
subprocess runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any, List, Optional, Sequence, Union


async def run_subprocess_async(
    cmd: Union[Sequence[str], List[str]],
    *,
    timeout: float = 30,
    cwd: Optional[str] = None,
    capture_output: bool = True,
    text: bool = True,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run a subprocess without blocking the asyncio event loop.

    Thin async wrapper around :func:`subprocess.run` that executes the
    blocking call in a thread via :func:`asyncio.to_thread`.

    Args:
        cmd: Command and arguments as a sequence of strings.
        timeout: Timeout in seconds (forwarded to ``subprocess.run``).
        cwd: Working directory for the subprocess.
        capture_output: If ``True``, capture stdout and stderr.
        text: If ``True``, return stdout/stderr as text (not bytes).
        **kwargs: Additional keyword arguments forwarded to
            :func:`subprocess.run`.

    Returns:
        The :class:`subprocess.CompletedProcess` result.

    Raises:
        subprocess.TimeoutExpired: If the subprocess does not finish
            within ``timeout`` seconds.
        FileNotFoundError: If the executable does not exist.
    """
    return await asyncio.to_thread(
        subprocess.run,
        list(cmd),
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        cwd=cwd,
        **kwargs,
    )
