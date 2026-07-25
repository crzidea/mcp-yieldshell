"""Unit tests for security module."""

import os
from pathlib import Path

import pytest

from mcp_yieldshell.config import Config
from mcp_yieldshell.security import (
    StreamingRedactor,
    build_env,
    redact_text,
    resolve_cwd,
    validate_command,
)


class TestValidateCommand:
    def test_no_rules_allows_all(self):
        config = Config()
        assert validate_command(config, "rm -rf /") is None

    def test_deny_regex_rejects(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"rm\s+-rf")
        config = Config()
        error = validate_command(config, "rm -rf /")
        assert error is not None
        assert "denied" in error.lower()
        assert "rm -rf" not in error

    def test_deny_regex_allows_non_matching(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"rm\s+-rf")
        config = Config()
        assert validate_command(config, "ls -la") is None

    def test_allow_regex_rejects_non_matching(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOW_COMMAND_REGEX", r"^git\s+")
        config = Config()
        error = validate_command(config, "ls -la")
        assert error is not None
        assert "not allowed" in error.lower()

    def test_allow_regex_allows_matching(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOW_COMMAND_REGEX", r"^git\s+")
        config = Config()
        assert validate_command(config, "git status") is None


class TestResolveCwd:
    def test_default_cwd(self):
        config = Config()
        path, error = resolve_cwd(config, None)
        assert error is None
        assert path == str(Path(os.getcwd()).resolve())

    def test_explicit_cwd(self):
        config = Config()
        path, error = resolve_cwd(config, "/tmp")
        assert error is None
        assert "/tmp" in path

    def test_cwd_under_allowed_root(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/tmp")
        config = Config()
        path, error = resolve_cwd(config, "/tmp")
        assert error is None

    def test_cwd_not_under_allowed_root(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/tmp")
        config = Config()
        path, error = resolve_cwd(config, "/etc")
        assert error is not None
        assert "not under allowed roots" in error

    def test_descendant_and_multiple_roots_are_allowed(self, monkeypatch, tmp_path):
        first = tmp_path / "first"
        child = first / "child"
        second = tmp_path / "second"
        child.mkdir(parents=True)
        second.mkdir()
        monkeypatch.setenv(
            "YIELDSHELL_ALLOWED_CWDS", os.pathsep.join((str(first), str(second)))
        )
        config = Config()

        assert resolve_cwd(config, str(child))[1] is None
        assert resolve_cwd(config, str(second))[1] is None

    def test_similarly_prefixed_sibling_and_traversal_are_rejected(
        self, monkeypatch, tmp_path
    ):
        allowed = tmp_path / "projects"
        sibling = tmp_path / "projects-private"
        allowed.mkdir()
        sibling.mkdir()
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", str(allowed))
        config = Config()

        assert resolve_cwd(config, str(sibling))[1] is not None
        assert resolve_cwd(config, str(allowed / ".." / sibling.name))[1] is not None

    def test_symlink_descendant_allowed_and_escape_rejected(self, monkeypatch, tmp_path):
        allowed = tmp_path / "allowed"
        child = allowed / "child"
        outside = tmp_path / "outside"
        child.mkdir(parents=True)
        outside.mkdir()
        inside_link = allowed / "inside-link"
        outside_link = allowed / "outside-link"
        inside_link.symlink_to(child, target_is_directory=True)
        outside_link.symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", str(allowed))
        config = Config()

        path, error = resolve_cwd(config, str(inside_link))
        assert error is None
        assert path == str(child.resolve())
        assert resolve_cwd(config, str(outside_link))[1] is not None

    def test_invalid_allowed_root_returns_structured_error(self, monkeypatch):
        original_resolve = Path.resolve

        def resolve(self, *args, **kwargs):
            if str(self) == "/bad/root":
                raise OSError("simulated resolution failure")
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/bad/root")
        config = Config()
        path, error = resolve_cwd(config, None)
        assert error is not None
        assert "Invalid allowed cwd root '/bad/root'" in error
        assert path == str(Path(config.default_cwd).resolve())


class TestBuildEnv:
    def test_overlay_merges(self):
        config = Config()
        env = build_env(config, {"MY_VAR": "hello"})
        assert env["MY_VAR"] == "hello"

    def test_overlay_overrides(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        config = Config()
        env = build_env(config, {"PATH": "/custom"})
        assert env["PATH"] == "/custom"

    def test_no_overlay_preserves_parent(self):
        config = Config()
        env = build_env(config, None)
        assert "PATH" in env


def test_redaction_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "secretvalue123")
    config = Config()

    assert redact_text(config, "output with secretvalue123 inside") == (
        "output with secretvalue123 inside"
    )


def test_streaming_redactor_empty_secret_fast_path_does_not_buffer_markers():
    redactor = StreamingRedactor(())
    text = "x" * 1_000_000 + "[REDACTED:UNFINISHED"

    assert redactor.feed(text) is text
    assert redactor.feed("", final=True) == ""


class TestRedactText:
    @pytest.fixture(autouse=True)
    def _enable_redaction(self, monkeypatch):
        monkeypatch.setenv(
            "YIELDSHELL_REDACT_ENV_REGEX", r"TOKEN|KEY|SECRET|PASSWORD"
        )

    def test_redacts_matching_env_values(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "secretvalue123")
        config = Config()
        result = redact_text(config, "output with secretvalue123 inside")
        assert "[REDACTED:MY_SECRET]" in result
        assert "secretvalue123" not in result

    def test_preserves_non_matching(self, monkeypatch):
        monkeypatch.setenv("MY_NORMAL_VAR", "normalvalue")
        config = Config()
        result = redact_text(config, "output with normalvalue inside")
        assert result == "output with normalvalue inside"

    def test_only_values_of_at_least_eight_characters_are_redacted(self, monkeypatch):
        monkeypatch.setenv("SHORT_SECRET", "1234567")
        monkeypatch.setenv("LONG_SECRET", "12345678")
        config = Config()
        result = redact_text(config, "1234567 12345678")
        assert result == "1234567 [REDACTED:LONG_SECRET]"

    def test_empty_matching_value_does_not_change_output(self, monkeypatch):
        monkeypatch.setenv("EMPTY_SECRET", "")
        config = Config()
        assert redact_text(config, "ordinary output") == "ordinary output"

    def test_custom_regex_selects_names(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_REDACT_ENV_REGEX", "^PRIVATE_")
        monkeypatch.setenv("PRIVATE_VALUE", "private-value")
        monkeypatch.setenv("PUBLIC_SECRET", "public-value")
        config = Config()
        result = redact_text(config, "private-value public-value")
        assert result == "[REDACTED:PRIVATE_VALUE] public-value"

    def test_overlapping_values_redact_longest_first(self, monkeypatch):
        monkeypatch.setenv("SHORT_SECRET", "abcdefgh")
        monkeypatch.setenv("LONG_SECRET", "xxabcdefghyy")
        config = Config()
        assert redact_text(config, "xxabcdefghyy") == "[REDACTED:LONG_SECRET]"

    def test_parent_environment_changes_do_not_change_snapshot(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "original-value")
        config = Config()
        monkeypatch.setenv("MY_SECRET", "changed-value")
        monkeypatch.setenv("NEW_SECRET", "another-value")

        result = redact_text(config, "original-value changed-value another-value")
        assert result == "[REDACTED:MY_SECRET] changed-value another-value"

    def test_marker_shaped_secret_is_redacted_before_marker_stash(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_REDACT_ENV_REGEX", "EVIL")
        monkeypatch.setenv("EVIL", "[REDACTED:TOKEN]")
        config = Config()
        result = redact_text(config, "x [REDACTED:TOKEN] y")
        assert "[REDACTED:EVIL]" in result
        assert "[REDACTED:TOKEN]" not in result
    def test_unrelated_substrings_matching_secret_fragments_are_not_redacted(
        self, monkeypatch
    ):
        monkeypatch.setenv("MY_SECRET", "abcdefghijklmnop")
        config = Config()
        leaked = "ijklmnop"
        result = redact_text(config, f"prefix {leaked}")
        assert leaked in result
        assert "[REDACTED:MY_SECRET]" not in result
    def test_existing_redaction_markers_are_not_rewritten(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "secretvalue123")
        config = Config()
        marker = "[REDACTED:MY_SECRET]"
        result = redact_text(config, f"keep {marker} intact")
        assert result == f"keep {marker} intact"

    def test_placeholder_shaped_output_is_not_corrupted(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "secretvalue123")
        config = Config()
        decoy = "\x1eredact-placeholder-0\x1e"
        result = redact_text(config, f"prefix {decoy} suffix")
        assert result == f"prefix {decoy} suffix"

    def test_configured_regex_matches_token(self):
        config = Config()
        assert config.redact_env_regex is not None
        assert config.redact_env_regex.search("API_TOKEN")
        assert config.redact_env_regex.search("PRIVATE_KEY")

    def test_streaming_redactor_matches_across_chunks(self):
        redactor = StreamingRedactor((("API_TOKEN", "secret-value"),))

        output = (
            redactor.feed("before secret")
            + redactor.feed("-value after")
            + redactor.feed("", final=True)
        )

        assert output == "before [REDACTED:API_TOKEN] after"

    def test_streaming_redactor_does_not_delay_unrelated_text(self):
        redactor = StreamingRedactor((("API_TOKEN", "secret-value"),))

        assert redactor.feed("ordinary output") == "ordinary output"
        assert redactor.feed("", final=True) == ""

    def test_streaming_redactor_preserves_marker_split_across_chunks(self):
        redactor = StreamingRedactor((("TOKEN", "MY_SECRET"),))

        output = (
            redactor.feed("[REDACTED:MY_")
            + redactor.feed("SECRET]")
            + redactor.feed("", final=True)
        )

        assert output == "[REDACTED:MY_SECRET]"

    def test_streaming_redactor_redacts_marker_shaped_secret(self):
        redactor = StreamingRedactor((("EVIL", "[REDACTED:TOKEN]"),))

        assert redactor.feed("[REDACTED:TOKEN]") == "[REDACTED:EVIL]"
        assert redactor.feed("", final=True) == ""

    def test_streaming_redactor_handles_large_unrelated_chunks(self):
        redactor = StreamingRedactor((("API_TOKEN", "secret-value"),))
        text = "ordinary output " * 50_000

        assert redactor.feed(text) == text
        assert redactor.feed("", final=True) == ""
