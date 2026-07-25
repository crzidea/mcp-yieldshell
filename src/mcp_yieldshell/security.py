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
        self._sensitive_env = tuple(
            sorted(sensitive_env, key=lambda item: (-len(item[1]), item[0], item[1]))
        )
        self._pending = ""
        self._secret_names: dict[str, str] = {}
        values: list[str] = []
        for name, value in self._sensitive_env:
            if value not in self._secret_names:
                self._secret_names[value] = name
                values.append(value)
        if values:
            secrets_pattern = "|".join(re.escape(value) for value in values)
            self._scan_re: re.Pattern[str] | None = re.compile(
                rf"(?P<secret>{secrets_pattern})|"
                rf"(?P<marker>{_REDACTED_MARKER_RE.pattern})"
            )
        else:
            self._scan_re = None

    def feed(self, text: str, *, final: bool = False) -> str:
        if not self._sensitive_env:
            # Marker handling only protects markers emitted by redaction. With no
            # secrets selected there is nothing to redact or carry across chunks.
            return text

        self._pending += text
        retained = 0 if final else self._incomplete_suffix_length()
        ready_end = len(self._pending) - retained
        ready = self._pending[:ready_end]
        self._pending = self._pending[ready_end:]
        if not ready:
            return ""

        assert self._scan_re is not None

        def replace(match: re.Match[str]) -> str:
            if match.lastgroup == "marker":
                return _redact_marker_secrets(
                    match.group(0),
                    self._sensitive_env,
                )
            value = match.group(0)
            return f"[REDACTED:{self._secret_names[value]}]"

        return self._scan_re.sub(replace, ready)

    def _incomplete_suffix_length(self) -> int:
        """Return the longest suffix that may complete a secret or marker."""
        pending = self._pending
        retained = 0
        for _, value in self._sensitive_env:
            limit = min(len(value) - 1, len(pending))
            for length in range(limit, retained, -1):
                if pending.endswith(value[:length]):
                    retained = length
                    break

        marker_start = pending.rfind(_MARKER_PREFIX)
        if marker_start >= 0:
            marker_suffix = pending[marker_start:]
            if (
                "]" not in marker_suffix
                and len(marker_suffix) <= _MAX_PENDING_MARKER
            ):
                retained = max(retained, len(marker_suffix))

        limit = min(len(_MARKER_PREFIX) - 1, len(pending))
        for length in range(limit, retained, -1):
            if pending.endswith(_MARKER_PREFIX[:length]):
                retained = length
                break
        return retained


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
    if overlay and config.redact_env_regex is not None:
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
    if not selected:
        return text

    placeholders: dict[str, str] = {}

    def _stash_marker(match: re.Match[str]) -> str:
        marker = match.group(0)
        redacted = _redact_marker_secrets(marker, selected)
        if redacted != marker:
            return redacted
        token = secrets.token_hex(16)
        placeholders[token] = marker
        return f"{_PLACEHOLDER_PREFIX}{token}\x1e"

    text = _REDACTED_MARKER_RE.sub(_stash_marker, text)
    for name, value in selected:
        text = text.replace(value, f"[REDACTED:{name}]")
    for token, marker in placeholders.items():
        text = text.replace(f"{_PLACEHOLDER_PREFIX}{token}\x1e", marker)
    return text


def _redact_marker_secrets(marker: str, sensitive_env: SensitiveEnv) -> str:
    """Preserve harmless markers without allowing them to conceal a secret."""
    for name, value in sorted(
        sensitive_env,
        key=lambda item: (-len(item[1]), item[0], item[1]),
    ):
        if value in marker:
            return f"[REDACTED:{name}]"
    return marker
