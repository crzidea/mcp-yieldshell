"""Configuration parsed from environment variables at server startup."""

from __future__ import annotations

import os
import re

from .types import DEFAULT_BLOCKED_SIDE_EFFECTS, SideEffect


class Config:
    def __init__(self) -> None:
        self.default_cwd: str = os.environ.get("YIELDSHELL_DEFAULT_CWD", os.getcwd())
        self.allowed_cwd_roots: list[str] = _parse_pathsep(
            os.environ.get("YIELDSHELL_ALLOWED_CWDS", "")
        )
        self.max_output_bytes: int = _parse_int(
            os.environ.get("YIELDSHELL_MAX_OUTPUT_BYTES", ""), 20000
        )
        self.max_processes: int = _parse_int(
            os.environ.get("YIELDSHELL_MAX_PROCESSES", ""), 50
        )
        self.default_yield_ms: int = _parse_int(
            os.environ.get("YIELDSHELL_DEFAULT_YIELD_MS", ""), 5000
        )
        self.max_yield_ms: int = _parse_int(
            os.environ.get("YIELDSHELL_MAX_YIELD_MS", ""), 300000
        )
        self.default_timeout_ms: int = _parse_int(
            os.environ.get("YIELDSHELL_DEFAULT_TIMEOUT_MS", ""), 0
        )
        self.deny_command_regex: re.Pattern[str] | None = _parse_regex(
            os.environ.get("YIELDSHELL_DENY_COMMAND_REGEX", "")
        )
        self.allow_command_regex: re.Pattern[str] | None = _parse_regex(
            os.environ.get("YIELDSHELL_ALLOW_COMMAND_REGEX", "")
        )
        self.redact_env_regex: re.Pattern[str] = _parse_regex_required(
            os.environ.get("YIELDSHELL_REDACT_ENV_REGEX", ""),
            r"TOKEN|KEY|SECRET|PASSWORD",
        )
        self.blocked_side_effects: frozenset[SideEffect] = _parse_blocked_side_effects(
            os.environ.get("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "")
        )


def _parse_pathsep(value: str) -> list[str]:
    if not value:
        return []
    return [p for p in value.split(os.pathsep) if p]


def _parse_int(value: str, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _parse_regex(value: str) -> re.Pattern[str] | None:
    if not value:
        return None
    return re.compile(value)


def _parse_regex_required(value: str, default: str) -> re.Pattern[str]:
    if not value:
        return re.compile(default)
    return re.compile(value)


def _parse_blocked_side_effects(value: str) -> frozenset[SideEffect]:
    """Parse ``MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS`` into a frozenset.

    Empty entries are ignored. Surrounding whitespace is trimmed. Names are
    case-sensitive. Invalid names raise ``ValueError`` with a clear message.
    """
    if value is None or not value.strip():
        return DEFAULT_BLOCKED_SIDE_EFFECTS

    valid_names = {member.name for member in SideEffect}
    blocked: set[SideEffect] = set()
    invalid: list[str] = []
    for entry in value.split(","):
        name = entry.strip()
        if not name:
            continue
        if name in valid_names:
            blocked.add(SideEffect[name])
        else:
            invalid.append(name)

    if invalid:
        names_list = ", ".join(repr(n) for n in invalid)
        valid_list = ", ".join(sorted(valid_names))
        raise ValueError(
            f"MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS contains invalid value(s): "
            f"{names_list}. Valid values (case-sensitive): {valid_list}."
        )

    return frozenset(blocked)
