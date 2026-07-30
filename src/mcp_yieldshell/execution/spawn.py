"""Platform-isolated OS process spawn helpers."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any


async def spawn_process(
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    shell: str | None = None,
) -> asyncio.subprocess.Process:
    """Spawn a subprocess shell command with stdout/stderr/stdin pipes.

    On POSIX, starts a new session so the shell and children form a
    process group that can be terminated together.
    """
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "stdin": asyncio.subprocess.PIPE,
    }
    if cwd:
        kwargs["cwd"] = cwd
    if env:
        kwargs["env"] = env
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    if shell:
        kwargs["executable"] = shell

    proc = await asyncio.create_subprocess_shell(command, **kwargs)
    return proc


def get_signal(name: str) -> signal.Signals | None:
    """Map a signal name string to its OS signal value, or return None."""
    name = name.upper()
    if not name.startswith("SIG"):
        name = f"SIG{name}"
    try:
        return signal.Signals[name]
    except (KeyError, ValueError):
        return None


async def terminate_process(
    proc: asyncio.subprocess.Process, process_group_id: int | None = None
) -> None:
    """Send SIGTERM (or equivalent) to the process group."""
    if sys.platform == "win32":
        try:
            proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass
    else:
        pid = proc.pid
        if pid is not None:
            try:
                os.killpg(_process_group_id(pid, process_group_id), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.terminate()
                except (ProcessLookupError, PermissionError):
                    pass


async def kill_process(
    proc: asyncio.subprocess.Process, process_group_id: int | None = None
) -> None:
    """Force kill the process group."""
    if sys.platform == "win32":
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
    else:
        pid = proc.pid
        if pid is not None:
            try:
                os.killpg(_process_group_id(pid, process_group_id), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except (ProcessLookupError, PermissionError):
                    pass


def _process_group_id(pid: int, process_group_id: int | None) -> int:
    if process_group_id is not None:
        return process_group_id
    # POSIX processes created by spawn_process() start a new session and are
    # therefore process-group leaders whose PGID equals their PID.
    return pid
