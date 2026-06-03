"""FastMCP-style MCP server wiring for YieldShell tools."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import Config
from .process.manager import ProcessManager
from .types import SideEffect

mcp = FastMCP("YieldShell MCP")

# Module-level manager, initialized once at startup
_manager: ProcessManager | None = None


def _get_manager() -> ProcessManager:
    if _manager is None:
        raise RuntimeError("Server not initialized")
    return _manager


@mcp.tool()
async def exec(
    command: str,
    side_effects: list[SideEffect],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    shell: str | None = None,
    stdin: str | None = None,
    name: str | None = None,
    yield_ms: int | None = None,
    timeout_ms: int | None = None,
    max_output_bytes: int | None = None,
) -> dict:
    """Execute a shell command with auto-yield for long-running processes.

    ``side_effects`` is required and must be a non-empty list. Declare every
    plausible side-effect category before running the command. ``NONE`` is
    exclusive and must not be combined with any other category; pass
    ``[SideEffect.NONE]`` (or ``["NONE"]``) only when no meaningful side
    effect is expected.

    The server rejects the call with ``failed_to_start`` (and stops before
    cwd validation, command policy, process-limit checks, env construction,
    and spawn) if any declared category is configured as blocked. By
    default, ``KILLS_AGENT_PROCESS``, ``MODIFIES_OS_SETTINGS``,
    ``MODIFIES_OS_USER_SETTINGS``, ``MODIFIES_PROTECTED_FILES``, and
    ``RUNS_INLINE_CODE`` are blocked. The error message names
    each blocked category, states that execution was stopped by policy
    before the process started, and gives a category-specific safer next
    action.

    ``RUNS_INLINE_CODE`` covers commands that execute code supplied inline
    to an interpreter or shell (e.g. ``python -c``, ``node -e``,
    ``curl ... | sh``). It does not cover simply creating a script or
    executable file unless the same command also executes inline code.
    When a command falls into that category, prefer writing the content to
    a reviewable workspace file and executing it in a small, inspectable
    step rather than piping or inlining it as a single ``exec`` call.
    ``RUNS_INLINE_CODE`` is in the default blocklist; the safer next
    action is to write a reviewable file and then run it.

    Concise examples (full guidance and the complete side-effect taxonomy
    live in the README):

    *   Read-only command: ``side_effects=["NONE"]``
    *   Workspace write: ``side_effects=["MODIFIES_WORKSPACE_FILES"]``
    *   Dependency install: ``side_effects=["CHANGES_PACKAGES_OR_DEPENDENCIES",
        "MAKES_NETWORK_REQUESTS"]``
    *   Network access: ``side_effects=["MAKES_NETWORK_REQUESTS"]``
    *   Destructive file ops: ``side_effects=["DELETES_FILES"]``
    *   Privileged command: ``side_effects=["RUNS_PRIVILEGED_COMMANDS"]``
    *   Protected-file change: ``side_effects=["MODIFIES_PROTECTED_FILES"]``
    *   Inline code execution: prefer to write the content to a
        reviewable file and run it; declaring
        ``side_effects=["RUNS_INLINE_CODE"]`` will be rejected
        under the default policy.
    """
    return await _get_manager().exec_command(
        command=command,
        side_effects=side_effects,
        cwd=cwd,
        env_overlay=env,
        shell=shell,
        stdin=stdin,
        name=name,
        yield_ms=yield_ms,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
    )


@mcp.tool()
async def read(
    process_id: str,
    since_seq: int | None = None,
    max_output_bytes: int | None = None,
    streams: str = "both",
) -> dict:
    """Read output from a managed process. Use since_seq for incremental reads."""
    return await _get_manager().read_output(
        process_id=process_id,
        since_seq=since_seq,
        max_output_bytes=max_output_bytes,
        streams=streams,
    )


@mcp.tool()
async def write(process_id: str, input: str, newline: bool = False) -> dict:
    """Write to the stdin of a running process."""
    return await _get_manager().write_input(
        process_id=process_id,
        input_data=input,
        newline=newline,
    )


@mcp.tool()
async def wait(
    process_id: str,
    timeout_ms: int = 30000,
    max_output_bytes: int | None = None,
) -> dict:
    """Wait for a process to exit without killing it."""
    return await _get_manager().wait_process(
        process_id=process_id,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
    )


@mcp.tool()
async def stop(
    process_id: str,
    signal: str = "SIGTERM",
    force_after_ms: int = 3000,
) -> dict:
    """Stop a running process with graceful termination then force kill."""
    return await _get_manager().stop_process(
        process_id=process_id,
        signal_name=signal,
        force_after_ms=force_after_ms,
    )


@mcp.tool()
async def ps(include_completed: bool = True, limit: int = 50) -> dict:
    """List managed processes."""
    return _get_manager().list_processes(
        include_completed=include_completed,
        limit=limit,
    )


@mcp.tool()
async def cleanup(
    completed_older_than_ms: int = 3600000,
    stopped_older_than_ms: int = 3600000,
) -> dict:
    """Remove completed/stopped processes older than thresholds."""
    return await _get_manager().cleanup(
        completed_older_than_ms=completed_older_than_ms,
        stopped_older_than_ms=stopped_older_than_ms,
    )


def create_server(config: Config | None = None) -> FastMCP:
    """Create and return the MCP server with the given config."""
    global _manager
    if config is None:
        config = Config()
    _manager = ProcessManager(config)
    return mcp
