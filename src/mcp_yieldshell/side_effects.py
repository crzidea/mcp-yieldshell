"""Validation and policy guidance for declared command side effects."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .types import SideEffect

_BLOCKED_CATEGORY_GUIDANCE: dict[SideEffect, str] = {
    SideEffect.MODIFIES_PROTECTED_FILES: (
        "Do not modify protected files; use a workspace-scoped path or ask "
        "the operator to update the policy via MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS."
    ),
    SideEffect.MODIFIES_OS_SETTINGS: (
        "Do not change OS-level configuration such as systemd units, kernel "
        "parameters, /etc files, package manager system config, or global "
        "service defaults; re-declare with a safer category or request an "
        "explicit policy override."
    ),
    SideEffect.MODIFIES_OS_USER_SETTINGS: (
        "Do not change user-level configuration such as shell rc files, XDG "
        "config directories, dotfiles, or per-user application preferences; "
        "re-declare with a safer category or request an explicit policy "
        "override."
    ),
    SideEffect.DELETES_FILES: (
        "Avoid deletion; prefer reversible edits, or request explicit "
        "confirmation when deletion is truly required."
    ),
    SideEffect.RUNS_INLINE_CODE: (
        "Do not execute code supplied inline to an interpreter or shell "
        "(e.g. python -c, node -e, curl ... | sh); create or edit a "
        "reviewable workspace file and execute it in a small, inspectable "
        "step."
    ),
    SideEffect.KILLS_AGENT_PROCESS: (
        "Do not run commands that may terminate the MCP client, agent, or "
        "related process running the agent workflow; re-declare with a "
        "safer category or request an explicit policy override."
    ),
}


def validate_side_effects(
    side_effects: Iterable[Any] | None,
    blocked: frozenset[SideEffect],
) -> str | None:
    """Return a user-facing validation error, or ``None`` when allowed."""
    try:
        declared = _normalize_side_effects(side_effects)
    except ValueError as exc:
        return f"Invalid side_effects: {exc}"
    except TypeError as exc:
        return str(exc)
    if declared is None:
        return "side_effects is required"
    if not declared:
        return (
            'side_effects must not be empty; pass ["NONE"] for commands '
            "with no side effects"
        )
    has_none = SideEffect.NONE in declared
    non_none = [item for item in declared if item is not SideEffect.NONE]
    if has_none and non_none:
        return (
            "side_effects=NONE is exclusive and cannot be combined with other "
            f"categories: {[item.name for item in non_none]}"
        )

    hits = [item for item in declared if item in blocked]
    return _format_blocked_message(hits) if hits else None


def _normalize_side_effects(
    side_effects: Iterable[Any] | None,
) -> list[SideEffect] | None:
    if side_effects is None:
        return None
    normalized: list[SideEffect] = []
    for item in side_effects:
        if isinstance(item, SideEffect):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append(SideEffect(item))
        else:
            raise TypeError(
                "side_effects entries must be SideEffect or str, "
                f"got {type(item).__name__}"
            )
    return normalized


def _format_blocked_message(blocked: list[SideEffect]) -> str:
    names = ", ".join(item.name for item in blocked)
    parts = [
        f"Side-effect category blocked by policy: {names}. "
        "Execution was stopped by policy before the process started."
    ]
    for category in blocked:
        guidance = _BLOCKED_CATEGORY_GUIDANCE.get(category)
        if guidance:
            parts.append(f"{category.name}: {guidance}")
        else:
            parts.append(
                f"{category.name}: re-declare with an allowed category or "
                "update operator policy."
            )
    return " ".join(parts)
