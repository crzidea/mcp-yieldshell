"""FastMCP-style MCP server wiring for YieldShell tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from .config import Config
from .execution.manager import ExecutionManager
from .policy import GRACEFUL_STOP_MS, MAX_EFFECTIVE_WAIT_MS
from .types import SideEffect

# Module-level manager, initialized once at startup
_manager: ExecutionManager | None = None


@asynccontextmanager
async def _server_lifespan(_: FastMCP) -> AsyncIterator[None]:
    manager = _manager
    try:
        yield
    finally:
        if manager is not None:
            await manager.shutdown()


mcp = FastMCP("YieldShell MCP", lifespan=_server_lifespan)


def _get_manager() -> ExecutionManager:
    if _manager is None:
        raise RuntimeError("Server not initialized")
    return _manager


@mcp.tool()
async def execute(
    command: str,
    side_effects: list[SideEffect],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    shell: str | None = None,
    stdin: str | None = None,
    close_stdin: bool = True,
    name: str | None = None,
    yield_ms: int | None = None,
    timeout_ms: int | None = None,
    max_output_bytes: int | None = None,
) -> dict:
    """Start a managed shell execution with automatic backgrounding.

    Addressable responses include an opaque ``execution_id``. Pass that value
    through unchanged to ``read``, ``write``, ``wait``, or ``stop``; never
    parse it or substitute the numeric OS ``process_id``.

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
    step rather than piping or inlining it as a single ``execute`` call.
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

    Standard input is closed after ``stdin`` is written by default so
    EOF-driven commands can complete. Set ``close_stdin=False`` when later
    calls to ``write`` are expected.
    """
    return await _get_manager().execute_command(
        command=command,
        side_effects=side_effects,
        cwd=cwd,
        env_overlay=env,
        shell=shell,
        stdin=stdin,
        close_stdin=close_stdin,
        name=name,
        yield_ms=yield_ms,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
    )


@mcp.tool()
async def read(
    execution_id: str,
    since_seq: int | None = None,
    max_output_bytes: int | None = None,
    streams: str = "both",
    tail_lines: int | None = None,
) -> dict:
    """Read output from a managed execution.

    ``execution_id`` is opaque and must be passed through unchanged from an
    addressable ``execute`` response.

    Pass ``since_seq`` (the ``next_seq`` from the previous response) to get
    only output produced since that point. Pass ``tail_lines`` instead to get
    a snapshot of the newest N lines, which is the cheaper way to monitor a
    noisy build or test run. The two are mutually exclusive.

    With neither, the read **resumes** from where the last cursorless read
    or ``execute`` snapshot stopped and then advances that server-side cursor,
    so repeated calls never return the same bytes twice and no cursor
    bookkeeping is needed. Because such a read consumes, inspecting output
    that was already delivered needs an explicit ``since_seq=1``.

    Requests that pass ``since_seq``, ``tail_lines``, or a narrowed
    ``streams`` are out-of-band: they neither consult nor move that cursor,
    so a peek cannot skip output the polling stream has not seen.

    Responses report withheld output with two distinct flags: ``capped``
    means more output is available and reading again continues from
    ``next_seq``, while ``evicted`` means output was dropped from the buffer
    before it could be read and is gone. ``latest_seq`` shows how far output
    has advanced overall.
    """
    return await _get_manager().read_execution_output(
        execution_id=execution_id,
        since_seq=since_seq,
        max_output_bytes=max_output_bytes,
        streams=streams,
        tail_lines=tail_lines,
    )


@mcp.tool()
async def write(
    execution_id: str,
    input: str,
    newline: bool = False,
    close_stdin: bool = False,
) -> dict:
    """Write to an execution's stdin, optionally closing it to send EOF.

    ``execution_id`` is opaque and must be passed through unchanged from an
    addressable ``execute`` response.
    """
    return await _get_manager().write_input(
        execution_id=execution_id,
        input_data=input,
        newline=newline,
        close_stdin=close_stdin,
    )


@mcp.tool()
async def wait(
    execution_id: str,
    timeout_ms: int = MAX_EFFECTIVE_WAIT_MS,
    max_output_bytes: int | None = None,
    since_seq: int | None = None,
    tail_lines: int | None = None,
) -> dict:
    """Wait for an execution to exit without stopping it.

    ``execution_id`` is opaque and must be passed through unchanged from an
    addressable ``execute`` response.

    ``timeout_ms`` is a **maximum wait**, not an execution limit: it never
    stops the execution, and it is capped at 55,000ms to stay under
    typical MCP request timeouts. The response reports ``wait_result``
    (``exited`` or ``deadline_reached``), ``waited_ms``, and the effective
    ``max_wait_ms`` so a capped request is visible rather than silent. The
    separate execution limit that does stop an execution is
    ``execute(timeout_ms=...)``.

    Output follows the same rules as ``read``: with no ``since_seq`` and no
    ``tail_lines`` the response resumes from where the last cursorless read
    stopped and advances that cursor, so polling in a loop never returns the
    same bytes twice. Pass ``since_seq`` to drive the cursor yourself, or
    ``tail_lines`` for an out-of-band snapshot of the newest N lines.
    """
    return await _get_manager().wait_execution(
        execution_id=execution_id,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
        since_seq=since_seq,
        tail_lines=tail_lines,
    )


@mcp.tool()
async def stop(
    execution_id: str,
    signal: str = "SIGTERM",
    force_after_ms: int = GRACEFUL_STOP_MS,
) -> dict:
    """Stop a running execution, escalating to force kill when needed.

    ``execution_id`` is opaque and must be passed through unchanged from an
    addressable ``execute`` response.
    """
    return await _get_manager().stop_execution(
        execution_id=execution_id,
        signal_name=signal,
        force_after_ms=force_after_ms,
    )


@mcp.tool()
async def ps(include_completed: bool = True, limit: int = 50) -> dict:
    """List managed executions, including retained terminal executions.

    Each entry reports ``idle_ms`` (time since output last arrived, ``null``
    if none has), ``last_output_at``, and ``latest_seq`` alongside byte
    counts. ``execution_id`` is an opaque handle that must be passed through
    unchanged; ``process_id`` is the initial shell's numeric OS PID.
    """
    return _get_manager().list_executions(
        include_completed=include_completed,
        limit=limit,
    )


@mcp.tool()
async def cleanup(
    completed_older_than_ms: int = 3600000,
    stopped_older_than_ms: int = 3600000,
) -> dict:
    """Remove completed/stopped execution records older than the thresholds."""
    return await _get_manager().cleanup(
        completed_older_than_ms=completed_older_than_ms,
        stopped_older_than_ms=stopped_older_than_ms,
    )


def create_server(config: Config | None = None) -> FastMCP:
    """Initialize and return the module's singleton MCP server."""
    global _manager
    if _manager is not None and not _manager._shutdown_complete:
        raise RuntimeError("Server is already initialized")
    if config is None:
        config = Config()
    _manager = ExecutionManager(config)
    return mcp
