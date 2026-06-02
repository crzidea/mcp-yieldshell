"""Unit tests for configuration parsing."""

import os

import pytest

from mcp_yieldshell.config import Config
from mcp_yieldshell.types import DEFAULT_BLOCKED_SIDE_EFFECTS, SideEffect


class TestConfigDefaults:
    def test_default_cwd(self):
        config = Config()
        assert config.default_cwd == os.getcwd()

    def test_default_max_output_bytes(self):
        config = Config()
        assert config.max_output_bytes == 20000

    def test_default_max_processes(self):
        config = Config()
        assert config.max_processes == 50

    def test_default_yield_ms(self):
        config = Config()
        assert config.default_yield_ms == 5000

    def test_default_max_yield_ms(self):
        config = Config()
        assert config.max_yield_ms == 300000

    def test_default_timeout_ms(self):
        config = Config()
        assert config.default_timeout_ms == 0

    def test_empty_allowed_cwds(self):
        config = Config()
        assert config.allowed_cwd_roots == []

    def test_none_deny_regex(self):
        config = Config()
        assert config.deny_command_regex is None

    def test_none_allow_regex(self):
        config = Config()
        assert config.allow_command_regex is None

    def test_default_redact_regex(self):
        config = Config()
        assert config.redact_env_regex is not None
        assert config.redact_env_regex.search("MY_TOKEN")
        assert config.redact_env_regex.search("API_KEY")
        assert config.redact_env_regex.search("MY_SECRET")
        assert config.redact_env_regex.search("DB_PASSWORD")


class TestConfigFromEnv:
    def test_custom_max_output_bytes(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_OUTPUT_BYTES", "5000")
        config = Config()
        assert config.max_output_bytes == 5000

    def test_custom_max_processes(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "10")
        config = Config()
        assert config.max_processes == 10

    def test_custom_default_yield_ms(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DEFAULT_YIELD_MS", "2000")
        config = Config()
        assert config.default_yield_ms == 2000

    def test_deny_command_regex(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"rm\s+-rf")
        config = Config()
        assert config.deny_command_regex is not None
        assert config.deny_command_regex.search("rm -rf /")
        assert not config.deny_command_regex.search("ls -la")

    def test_allow_command_regex(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOW_COMMAND_REGEX", r"^git\s+")
        config = Config()
        assert config.allow_command_regex is not None
        assert config.allow_command_regex.search("git status")
        assert not config.allow_command_regex.search("ls -la")

    def test_allowed_cwds(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/tmp:/home")
        config = Config()
        assert len(config.allowed_cwd_roots) == 2

    def test_invalid_int_uses_default(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "abc")
        config = Config()
        assert config.max_processes == 50


class TestBlockedSideEffectsDefaults:
    def test_unset_uses_default_blocked_set(self, monkeypatch):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        assert config.blocked_side_effects == DEFAULT_BLOCKED_SIDE_EFFECTS

    def test_unset_default_blocks_modifies_protected_files(self, monkeypatch):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        assert SideEffect.MODIFIES_PROTECTED_FILES in config.blocked_side_effects

    def test_unset_default_blocks_breaks_operating_system(self, monkeypatch):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        assert SideEffect.BREAKS_OPERATING_SYSTEM in config.blocked_side_effects

    def test_empty_string_uses_default_blocked_set(self, monkeypatch):
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "")
        config = Config()
        assert config.blocked_side_effects == DEFAULT_BLOCKED_SIDE_EFFECTS

    def test_whitespace_only_uses_default_blocked_set(self, monkeypatch):
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "   ")
        config = Config()
        assert config.blocked_side_effects == DEFAULT_BLOCKED_SIDE_EFFECTS


class TestBlockedSideEffectsFromEnv:
    def test_single_value_parsed(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "DELETES_FILES"
        )
        config = Config()
        assert config.blocked_side_effects == frozenset({SideEffect.DELETES_FILES})

    def test_multiple_values_parsed(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS",
            "DELETES_FILES,MAKES_NETWORK_REQUESTS,INSTALLS_DEPENDENCIES",
        )
        config = Config()
        assert config.blocked_side_effects == frozenset(
            {
                SideEffect.DELETES_FILES,
                SideEffect.MAKES_NETWORK_REQUESTS,
                SideEffect.INSTALLS_DEPENDENCIES,
            }
        )

    def test_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS",
            "  DELETES_FILES  ,\t MAKES_NETWORK_REQUESTS \t",
        )
        config = Config()
        assert config.blocked_side_effects == frozenset(
            {SideEffect.DELETES_FILES, SideEffect.MAKES_NETWORK_REQUESTS}
        )

    def test_empty_entries_ignored(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS",
            ",,DELETES_FILES, ,,",
        )
        config = Config()
        assert config.blocked_side_effects == frozenset({SideEffect.DELETES_FILES})

    def test_can_clear_blocked_categories(self, monkeypatch):
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", ",")
        config = Config()
        assert config.blocked_side_effects == frozenset()

    def test_override_default_to_unblock_modifies_protected_files(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "BREAKS_OPERATING_SYSTEM"
        )
        config = Config()
        assert config.blocked_side_effects == frozenset(
            {SideEffect.BREAKS_OPERATING_SYSTEM}
        )


class TestBlockedSideEffectsInvalid:
    def test_lowercase_value_fails_clearly(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "modifies_protected_files"
        )
        with pytest.raises(ValueError) as excinfo:
            Config()
        message = str(excinfo.value)
        assert "modifies_protected_files" in message
        assert "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS" in message
        assert "case-sensitive" in message

    def test_typo_value_fails_clearly(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "DELETES_FILE"
        )
        with pytest.raises(ValueError) as excinfo:
            Config()
        message = str(excinfo.value)
        assert "DELETES_FILE" in message
        assert "DELETES_FILES" in message  # listed in the valid set

    def test_completely_unknown_value_fails_clearly(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "TELEPORT_COWS"
        )
        with pytest.raises(ValueError) as excinfo:
            Config()
        message = str(excinfo.value)
        assert "TELEPORT_COWS" in message
        assert "Valid values" in message

    def test_mixed_valid_and_invalid_fails_clearly(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS",
            "DELETES_FILES,TELEPORT_COWS,MAKES_NETWORK_REQUESTS",
        )
        with pytest.raises(ValueError) as excinfo:
            Config()
        message = str(excinfo.value)
        assert "TELEPORT_COWS" in message
