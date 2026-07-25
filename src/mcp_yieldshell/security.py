"""Security: command allow/deny, cwd validation, env overlay, and redaction."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from .config import Config

SensitiveEnv = tuple[tuple[str, str], ...]

_REDACTED_MARKER_RE = re.compile(r"\[REDACTED:[^\]]+\]")
_PLACEHOLDER_PREFIX = "\x1eredact-placeholder-"
_MARKER_PREFIX = "[REDACTED:"
_MAX_PENDING_MARKER = 1024


class StreamingRedactor:
    """Redact sensitive values across arbitrary text chunk boundaries."""

    def __init__(self, sensitive_env: SensitiveEnv) -> None:
        self._sensitive_env = sensitive_env
        self._pending = ""

    def feed(self, text: str, *, final: bool = False) -> str:
        self._pending += text
        output: list[str] = []
        position = 0
        while position < len(self._pending):
            secret = next(
                (
                    (name, value)
                    for name, value in self._sensitive_env
                    if self._pending.startswith(value, position)
                ),
                None,
            )
            if secret is not None:
                name, value = secret
                output.append(f"[REDACTED:{name}]")
                position += len(value)
                continue

            marker = _REDACTED_MARKER_RE.match(self._pending, position)
            if marker is not None:
                output.append(marker.group(0))
                position = marker.end()
                continue

            suffix = self._pending[position:]
            incomplete_marker = (
                _MARKER_PREFIX.startswith(suffix)
                or (
                    suffix.startswith(_MARKER_PREFIX)
                    and "]" not in suffix
                    and len(suffix) <= _MAX_PENDING_MARKER
                )
            )
            if not final and (
                incomplete_marker
                or any(
                    value.startswith(suffix) for _, value in self._sensitive_env
                )
            ):
                break

            output.append(self._pending[position])
            position += 1

        self._pending = self._pending[position:]
        return "".join(output)


def validate_command(config: Config, command: str) -> str | None:
    """Return an error message if the command is denied, else None."""
    if config.deny_command_regex and config.deny_command_regex.search(command):
        return "Command denied by policy"
    if config.allow_command_regex and not config.allow_command_regex.search(command):
        return "Command not allowed by policy"
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


def collect_sensitive_env(
    config: Config, overlay: dict[str, str] | None = None
) -> SensitiveEnv:
    """Return startup secrets plus sensitive values supplied in an overlay."""
    selected = list(config.sensitive_env)
    if overlay:
        selected.extend(
            (name, value)
            for name, value in overlay.items()
            if config.redact_env_regex.search(name) and len(value) >= 8
        )
    return tuple(
        sorted(
            set(selected),
            key=lambda item: (-len(item[1]), item[0], item[1]),
        )
    )


def redact_text(
    config: Config,
    text: str,
    sensitive_env: SensitiveEnv | None = None,
) -> str:
    """Best-effort redaction of sensitive environment values from text."""
    selected = config.sensitive_env if sensitive_env is None else sensitive_env
    # Marker-shaped secrets must be replaced before stashing markers; otherwise
    # the marker regex treats the secret as an existing marker and restores it.
    for name, value in selected:
        if _REDACTED_MARKER_RE.fullmatch(value):
            text = text.replace(value, f"[REDACTED:{name}]")

    placeholders: dict[str, str] = {}

    def _stash_marker(match: re.Match[str]) -> str:
        token = secrets.token_hex(16)
        placeholders[token] = match.group(0)
        return f"{_PLACEHOLDER_PREFIX}{token}\x1e"

    text = _REDACTED_MARKER_RE.sub(_stash_marker, text)
    for name, value in selected:
        text = text.replace(value, f"[REDACTED:{name}]")
    for token, marker in placeholders.items():
        text = text.replace(f"{_PLACEHOLDER_PREFIX}{token}\x1e", marker)
    return text
