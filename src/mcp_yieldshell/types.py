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

    ``RUNS_INLINE_CODE`` covers commands that execute code supplied inline
    to an interpreter or shell, such as ``python -c``, ``node -e``,
    ``ruby -e``, ``perl -e``, shell heredocs piped into interpreters, or
    ``curl ... | sh``. It does not cover simply creating a script or
    executable file unless the same command also executes inline code.
    Prefer writing such content to a reviewable workspace file and
    executing it in a small, inspectable step.

    ``MODIFIES_OS_SETTINGS`` covers commands that change OS-level
    configuration such as systemd units, kernel parameters, ``/etc``
    files, package manager system config, or global service defaults.

    ``MODIFIES_OS_USER_SETTINGS`` covers commands that change user-level
    configuration such as shell rc files, XDG config directories,
    dotfiles, or per-user application preferences.

    ``MODIFIES_SECURITY_CONTROLS`` covers commands that alter security
    posture such as firewall rules, SELinux/AppArmor policies, file
    permissions on security-sensitive paths, or authentication
    configuration.

    ``MODIFIES_PRODUCTION_SERVICES`` covers commands that affect live
    production services, databases, or deployed infrastructure.

    ``CHANGES_PACKAGES_OR_DEPENDENCIES`` covers commands that install,
    upgrade, remove, or otherwise modify package or dependency state.
    """

    CHANGES_NETWORK_CONFIGURATION = "CHANGES_NETWORK_CONFIGURATION"
    CHANGES_PACKAGES_OR_DEPENDENCIES = "CHANGES_PACKAGES_OR_DEPENDENCIES"
    CONSUMES_SIGNIFICANT_RESOURCES = "CONSUMES_SIGNIFICANT_RESOURCES"
    DELETES_FILES = "DELETES_FILES"
    EXPOSES_SECRETS = "EXPOSES_SECRETS"
    KILLS_AGENT_PROCESS = "KILLS_AGENT_PROCESS"
    MAKES_NETWORK_REQUESTS = "MAKES_NETWORK_REQUESTS"
    MODIFIES_OS_SETTINGS = "MODIFIES_OS_SETTINGS"
    MODIFIES_OS_USER_SETTINGS = "MODIFIES_OS_USER_SETTINGS"
    MODIFIES_OUTSIDE_WORKSPACE = "MODIFIES_OUTSIDE_WORKSPACE"
    MODIFIES_PRODUCTION_SERVICES = "MODIFIES_PRODUCTION_SERVICES"
    MODIFIES_PROTECTED_FILES = "MODIFIES_PROTECTED_FILES"
    MODIFIES_SECURITY_CONTROLS = "MODIFIES_SECURITY_CONTROLS"
    MODIFIES_WORKSPACE_FILES = "MODIFIES_WORKSPACE_FILES"
    NONE = "NONE"
    OTHER = "OTHER"
    RUNS_INLINE_CODE = "RUNS_INLINE_CODE"
    RUNS_PRIVILEGED_COMMANDS = "RUNS_PRIVILEGED_COMMANDS"
    STOPS_OR_RESTARTS_SERVICES = "STOPS_OR_RESTARTS_SERVICES"
    UNKNOWN = "UNKNOWN"
    USES_DESTRUCTIVE_GIT_OPERATION = "USES_DESTRUCTIVE_GIT_OPERATION"


DEFAULT_BLOCKED_SIDE_EFFECTS: frozenset[SideEffect] = frozenset(
    {
        SideEffect.KILLS_AGENT_PROCESS,
        SideEffect.MODIFIES_OS_SETTINGS,
        SideEffect.MODIFIES_OS_USER_SETTINGS,
        SideEffect.MODIFIES_PROTECTED_FILES,
        SideEffect.RUNS_INLINE_CODE,
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
