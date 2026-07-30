"""Linux containment helpers for descendants that leave their process group."""

from __future__ import annotations

import os
import secrets
import signal
import sys

EXECUTION_TOKEN_ENV = "_MCP_YIELDSHELL_EXECUTION_TOKEN"


def new_execution_token() -> str | None:
    """Return a process-discovery token on platforms that expose procfs."""
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc"):
        return None
    return secrets.token_hex(16)


def contained_process_ids(token: str | None) -> set[int]:
    """Find live processes whose initial environment carries ``token``."""
    if token is None:
        return set()
    expected = f"{EXECUTION_TOKEN_ENV}={token}".encode()
    found: set[int] = set()
    try:
        entries = os.scandir("/proc")
    except OSError:
        return found
    with entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                with open(f"/proc/{pid}/environ", "rb") as environ_file:
                    environment = environ_file.read()
            except (OSError, ValueError):
                continue
            if expected in environment.split(b"\0"):
                found.add(pid)
    return found


def contained_processes_exist(token: str | None) -> bool:
    """Return whether any process carrying ``token`` is still observable."""
    return bool(contained_process_ids(token))


def signal_contained_processes(
    token: str | None,
    sig: signal.Signals,
    *,
    exclude_process_group_id: int | None = None,
) -> None:
    """Signal tagged processes outside the original managed process group."""
    for pid in contained_process_ids(token):
        if exclude_process_group_id is not None:
            try:
                if os.getpgid(pid) == exclude_process_group_id:
                    continue
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
