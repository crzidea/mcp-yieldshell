"""Security: command allow/deny, cwd validation, env overlay, and redaction."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from .config import Config

_REDACTED_MARKER_RE = re.compile(r"\[REDACTED:[^\]]+\]")
_PLACEHOLDER_PREFIX = "\x1eredact-placeholder-"


def validate_command(config: Config, command: str) -> str | None:
    """Return an error message if the command is denied, else None."""
    if config.deny_command_regex and config.deny_command_regex.search(command):
        return f"Command denied by policy: {command[:80]}"
    if config.allow_command_regex and not config.allow_command_regex.search(command):
        return f"Command not allowed by policy: {command[:80]}"
    return None


def resolve_cwd(config: Config, requested_cwd: str | None) -> tuple[str, str | None]:
    """Resolve and validate cwd. Returns (resolved_path, error_or_None)."""
    target = requested_cwd or config.default_cwd
    try:
        resolved_path = Path(target).resolve()
    except Exception as exc:
        return target, f"Invalid cwd: {exc}"
    resolved = str(resolved_path)
    if config.allowed_cwd_roots:
        allowed_roots: list[Path] = []
        for value in config.allowed_cwd_roots:
            try:
                allowed_roots.append(Path(value).resolve())
            except Exception as exc:
                return resolved, f"Invalid allowed cwd root {value!r}: {exc}"
        if not any(
            resolved_path == root or resolved_path.is_relative_to(root)
            for root in allowed_roots
        ):
            return resolved, f"Cwd not under allowed roots: {resolved}"
    return resolved, None


def build_env(config: Config, overlay: dict[str, str] | None) -> dict[str, str]:
    """Merge overlay into parent env without exposing raw parent env in tool output."""
    env = dict(os.environ)
    if overlay:
        env.update(overlay)
    return env


def redact_text(config: Config, text: str) -> str:
    """Best-effort redaction of sensitive environment values from text."""
    # Marker-shaped secrets must be replaced before stashing markers; otherwise
    # the marker regex treats the secret as an existing marker and restores it.
    for name, value in config.sensitive_env:
        if _REDACTED_MARKER_RE.fullmatch(value):
            text = text.replace(value, f"[REDACTED:{name}]")

    placeholders: dict[str, str] = {}

    def _stash_marker(match: re.Match[str]) -> str:
        token = secrets.token_hex(16)
        placeholders[token] = match.group(0)
        return f"{_PLACEHOLDER_PREFIX}{token}\x1e"

    text = _REDACTED_MARKER_RE.sub(_stash_marker, text)
    for name, value in config.sensitive_env:
        text = text.replace(value, f"[REDACTED:{name}]")
    for token, marker in placeholders.items():
        text = text.replace(f"{_PLACEHOLDER_PREFIX}{token}\x1e", marker)
    return text
