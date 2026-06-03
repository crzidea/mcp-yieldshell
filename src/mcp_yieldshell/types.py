"""Types for process status, tool response shapes, and side-effect taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProcessStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class SideEffect(str, Enum):
    """Taxonomy of side effects a shell command may plausibly have.

    Callers of ``exec_command`` must declare every category that plausibly
    applies. If no meaningful side effect is expected, callers must pass
    ``[SideEffect.NONE]``. ``NONE`` is exclusive and must not be combined
    with any other category.

    ``GENERATES_EXECUTABLE_CONTENT`` covers opaque inline content that is
    difficult to inspect before execution: long generated code, scripts,
    shell pipelines, SQL, configuration, heredocs, encoded payloads, and
    generated files that are executed immediately. Prefer writing such
    content to a reviewable workspace file and executing it in a small,
    inspectable step.
    """

    NONE = "NONE"
    MODIFIES_WORKSPACE_FILES = "MODIFIES_WORKSPACE_FILES"
    MODIFIES_PROTECTED_FILES = "MODIFIES_PROTECTED_FILES"
    MODIFIES_OUTSIDE_WORKSPACE = "MODIFIES_OUTSIDE_WORKSPACE"
    DELETES_FILES = "DELETES_FILES"
    INSTALLS_DEPENDENCIES = "INSTALLS_DEPENDENCIES"
    CHANGES_SYSTEM_CONFIGURATION = "CHANGES_SYSTEM_CONFIGURATION"
    BREAKS_OPERATING_SYSTEM = "BREAKS_OPERATING_SYSTEM"
    AFFECTS_PRODUCTION_SERVICES = "AFFECTS_PRODUCTION_SERVICES"
    STOPS_OR_RESTARTS_SERVICES = "STOPS_OR_RESTARTS_SERVICES"
    EXPOSES_SECRETS = "EXPOSES_SECRETS"
    CREATES_SECURITY_RISK = "CREATES_SECURITY_RISK"
    CHANGES_NETWORK_CONFIGURATION = "CHANGES_NETWORK_CONFIGURATION"
    MAKES_NETWORK_REQUESTS = "MAKES_NETWORK_REQUESTS"
    RUNS_PRIVILEGED_COMMANDS = "RUNS_PRIVILEGED_COMMANDS"
    USES_DESTRUCTIVE_GIT_OPERATION = "USES_DESTRUCTIVE_GIT_OPERATION"
    CONSUMES_SIGNIFICANT_RESOURCES = "CONSUMES_SIGNIFICANT_RESOURCES"
    GENERATES_EXECUTABLE_CONTENT = "GENERATES_EXECUTABLE_CONTENT"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


DEFAULT_BLOCKED_SIDE_EFFECTS: frozenset[SideEffect] = frozenset(
    {
        SideEffect.MODIFIES_PROTECTED_FILES,
        SideEffect.BREAKS_OPERATING_SYSTEM,
        SideEffect.GENERATES_EXECUTABLE_CONTENT,
    }
)


@dataclass
class ProcessInfo:
    process_id: str
    pid: int | None
    command: str
    cwd: str
    name: str | None
    status: ProcessStatus
    exit_code: int | None = None
    signal: str | None = None
    started_at: float = 0.0
    ended_at: float | None = None
    duration_ms: float = 0.0
    start_monotonic: float = 0.0
