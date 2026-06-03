"""Tests for the required side-effect declaration on ``exec_command``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mcp_yieldshell.config import Config
from mcp_yieldshell.process.manager import ProcessManager
from mcp_yieldshell.server import exec
from mcp_yieldshell.types import SideEffect

NONE = [SideEffect.NONE]


def _exec_tool_schema() -> dict:
    """Return the JSON schema generated for the ``exec`` MCP tool."""
    from mcp.server.fastmcp.tools.base import Tool

    tool = Tool.from_function(exec, name="exec", description=exec.__doc__)
    return tool.parameters


class TestExecSchema:
    def test_side_effects_is_required(self):
        schema = _exec_tool_schema()
        required = schema.get("required", [])
        assert "side_effects" in required

    def test_side_effects_items_reference_side_effect_enum(self):
        schema = _exec_tool_schema()
        side_effects = schema["properties"]["side_effects"]
        assert side_effects["type"] == "array"
        items = side_effects["items"]
        assert "$ref" in items
        assert items["$ref"] == "#/$defs/SideEffect"

    def test_side_effect_enum_def_contains_all_canonical_names(self):
        schema = _exec_tool_schema()
        defs = schema["$defs"]["SideEffect"]
        enum_values = defs["enum"]
        expected = [member.value for member in SideEffect]
        assert enum_values == expected
        for canonical in (
            "CHANGES_NETWORK_CONFIGURATION",
            "CHANGES_PACKAGES_OR_DEPENDENCIES",
            "CONSUMES_SIGNIFICANT_RESOURCES",
            "DELETES_FILES",
            "EXPOSES_SECRETS",
            "KILLS_AGENT_PROCESS",
            "MAKES_NETWORK_REQUESTS",
            "MODIFIES_OS_SETTINGS",
            "MODIFIES_OS_USER_SETTINGS",
            "MODIFIES_OUTSIDE_WORKSPACE",
            "MODIFIES_PRODUCTION_SERVICES",
            "MODIFIES_PROTECTED_FILES",
            "MODIFIES_SECURITY_CONTROLS",
            "MODIFIES_WORKSPACE_FILES",
            "NONE",
            "OTHER",
            "RUNS_INLINE_CODE",
            "RUNS_PRIVILEGED_COMMANDS",
            "STOPS_OR_RESTARTS_SERVICES",
            "UNKNOWN",
            "USES_DESTRUCTIVE_GIT_OPERATION",
        ):
            assert canonical in enum_values

    def test_side_effect_enum_schema_order_is_alphabetical(self):
        schema = _exec_tool_schema()
        enum_values = schema["$defs"]["SideEffect"]["enum"]
        assert enum_values == sorted(enum_values)

    def test_side_effect_enum_def_is_type_string(self):
        schema = _exec_tool_schema()
        assert schema["$defs"]["SideEffect"]["type"] == "string"

    def test_schema_excludes_unknown_categories(self):
        schema = _exec_tool_schema()
        assert "TELEPORT_COWS" not in schema["$defs"]["SideEffect"]["enum"]

    def test_full_schema_serializes_to_json(self):
        schema = _exec_tool_schema()
        # The schema must be JSON-serializable for MCP transport.
        json.dumps(schema)


@pytest.fixture
def manager():
    return ProcessManager(Config())


class TestRuntimeSideEffectValidation:
    @pytest.mark.asyncio
    async def test_none_runs_command(self, manager):
        result = await manager.exec_command("echo hello", side_effects=NONE)
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_none_as_strings_runs_command(self, manager):
        result = await manager.exec_command("echo hello", side_effects=["NONE"])
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_empty_list_rejected(self, manager):
        result = await manager.exec_command("echo hello", side_effects=[])
        assert result["status"] == "failed_to_start"
        assert "side_effects" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_none_rejected(self, manager):
        result = await manager.exec_command("echo hello", side_effects=None)
        assert result["status"] == "failed_to_start"
        assert "required" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_unknown_value_rejected(self, manager):
        result = await manager.exec_command(
            "echo hello", side_effects=["TELEPORT_COWS"]
        )
        assert result["status"] == "failed_to_start"
        assert "TELEPORT_COWS" in result["error"]

    @pytest.mark.asyncio
    async def test_none_combined_with_other_rejected(self, manager):
        result = await manager.exec_command(
            "echo hello", side_effects=["NONE", "DELETES_FILES"]
        )
        assert result["status"] == "failed_to_start"
        assert "NONE" in result["error"]
        assert "DELETES_FILES" in result["error"]

    @pytest.mark.asyncio
    async def test_allowed_non_none_runs_command(self, manager):
        result = await manager.exec_command(
            "echo hello", side_effects=["MAKES_NETWORK_REQUESTS"]
        )
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]


class TestBlockedRejection:
    @pytest.mark.asyncio
    async def test_blocked_modifies_protected_files_rejected(self, monkeypatch):
        # No env var: default blocks MODIFIES_PROTECTED_FILES
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_PROTECTED_FILES"]
        )
        assert result["status"] == "failed_to_start"
        assert "MODIFIES_PROTECTED_FILES" in result["error"]
        assert "blocked by policy" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_blocked_modifies_os_settings_rejected(self, monkeypatch):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_OS_SETTINGS"]
        )
        assert result["status"] == "failed_to_start"
        assert "MODIFIES_OS_SETTINGS" in result["error"]

    @pytest.mark.asyncio
    async def test_rejection_names_all_blocked_categories(self, monkeypatch):
        # Configure both MODIFIES_PROTECTED_FILES and DELETES_FILES as blocked
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS",
            "MODIFIES_PROTECTED_FILES,DELETES_FILES",
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello",
            side_effects=["DELETES_FILES", "MODIFIES_PROTECTED_FILES"],
        )
        assert result["status"] == "failed_to_start"
        assert "DELETES_FILES" in result["error"]
        assert "MODIFIES_PROTECTED_FILES" in result["error"]

    @pytest.mark.asyncio
    async def test_blocked_does_not_create_process(self, monkeypatch, tmp_path):
        # Set up an allowed_cwd_roots that would otherwise reject the
        # command. If the blocked-category gate runs first, we get a
        # blocked-side-effect error rather than a cwd error.
        marker = tmp_path / "must_not_exist.txt"
        command = f"touch {marker}"
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/nonexistent_root_for_test")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "MODIFIES_PROTECTED_FILES"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(command, side_effects=["MODIFIES_PROTECTED_FILES"])
        assert result["status"] == "failed_to_start"
        assert "blocked by policy" in result["error"].lower()
        # Cwd check would have produced "Cwd not under allowed roots" — verify
        # the side-effect gate ran first.
        assert "not under allowed roots" not in result["error"]
        # And the process must not have run.
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_allowed_category_does_not_create_blocked_rejection(self, manager):
        result = await manager.exec_command(
            "echo hello", side_effects=["MAKES_NETWORK_REQUESTS"]
        )
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_blocked_rejection_does_not_register_process(self, monkeypatch):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        ps_before = mgr.list_processes(include_completed=False)["processes"]
        before_ids = {p["process_id"] for p in ps_before}

        result = await mgr.exec_command(
            "sleep 60", side_effects=["MODIFIES_PROTECTED_FILES"]
        )
        assert result["status"] == "failed_to_start"

        ps_after = mgr.list_processes(include_completed=False)["processes"]
        after_ids = {p["process_id"] for p in ps_after}
        assert after_ids == before_ids

    @pytest.mark.asyncio
    async def test_blocked_modifies_protected_feedback_safer_action(
        self, monkeypatch
    ):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_PROTECTED_FILES"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "MODIFIES_PROTECTED_FILES" in message
        assert "blocked by policy" in message.lower()
        assert "stopped by policy" in message.lower()
        # Category-specific corrective guidance:
        assert "workspace-scoped" in message.lower()
        assert "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS" in message

    @pytest.mark.asyncio
    async def test_blocked_modifies_os_settings_feedback_safer_action(
        self, monkeypatch
    ):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_OS_SETTINGS"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "MODIFIES_OS_SETTINGS" in message
        assert "stopped by policy" in message.lower()
        # Category-specific corrective guidance:
        assert "OS-level" in message or "systemd" in message or "/etc" in message
        assert "re-declare" in message.lower()

    @pytest.mark.asyncio
    async def test_blocked_deletes_files_feedback_safer_action(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "DELETES_FILES"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["DELETES_FILES"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "DELETES_FILES" in message
        assert "stopped by policy" in message.lower()
        # Category-specific corrective guidance:
        assert "deletion" in message.lower()
        assert "reversible" in message.lower()

    @pytest.mark.asyncio
    async def test_blocked_runs_inline_code_default_rejected(
        self, monkeypatch
    ):
        """The new category is in the default blocklist when env is unset."""
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["RUNS_INLINE_CODE"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "RUNS_INLINE_CODE" in message
        assert "stopped by policy" in message.lower()
        # Category-specific safer-next-action guidance:
        assert "reviewable" in message.lower()
        assert "workspace" in message.lower()
        assert "inspectable" in message.lower()

    @pytest.mark.asyncio
    async def test_blocked_runs_inline_code_unblocked_by_env(
        self, monkeypatch
    ):
        """Operators can clear the default blocklist to allow inline code."""
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", ",")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["RUNS_INLINE_CODE"]
        )
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_blocked_runs_inline_code_blocked_alongside_other(
        self, monkeypatch
    ):
        """Operators can reconfigure the blocklist to include the new category."""
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS",
            "DELETES_FILES,RUNS_INLINE_CODE",
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello",
            side_effects=["DELETES_FILES", "RUNS_INLINE_CODE"],
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "RUNS_INLINE_CODE" in message
        assert "DELETES_FILES" in message
        # The category-specific safer-next-action must be present for the
        # new category even when reported alongside another category.
        assert "reviewable" in message.lower()

    @pytest.mark.asyncio
    async def test_blocked_modifies_os_user_settings_default_rejected(
        self, monkeypatch
    ):
        """MODIFIES_OS_USER_SETTINGS is in the default blocklist when env is unset."""
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_OS_USER_SETTINGS"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "MODIFIES_OS_USER_SETTINGS" in message
        assert "stopped by policy" in message.lower()
        # Category-specific safer-next-action guidance:
        assert "user" in message.lower() or "settings" in message.lower()

    @pytest.mark.asyncio
    async def test_blocked_modifies_os_user_settings_unblocked_by_env(
        self, monkeypatch
    ):
        """Operators can clear the default blocklist to allow OS user settings."""
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", ",")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_OS_USER_SETTINGS"]
        )
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_blocked_modifies_os_user_settings_feedback_safer_action(
        self, monkeypatch
    ):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_OS_USER_SETTINGS"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "MODIFIES_OS_USER_SETTINGS" in message
        assert "stopped by policy" in message.lower()
        # Category-specific corrective guidance:
        assert "re-declare" in message.lower()

    @pytest.mark.asyncio
    async def test_blocked_kills_agent_process_default_rejected(
        self, monkeypatch
    ):
        """KILLS_AGENT_PROCESS is in the default blocklist when env is unset."""
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["KILLS_AGENT_PROCESS"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "KILLS_AGENT_PROCESS" in message
        assert "stopped by policy" in message.lower()
        # Category-specific safer-next-action guidance:
        assert "agent" in message.lower() or "mcp" in message.lower()

    @pytest.mark.asyncio
    async def test_blocked_kills_agent_process_unblocked_by_env(
        self, monkeypatch
    ):
        """Operators can clear the default blocklist to allow agent process."""
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", ",")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["KILLS_AGENT_PROCESS"]
        )
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_blocked_kills_agent_process_feedback_safer_action(
        self, monkeypatch
    ):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["KILLS_AGENT_PROCESS"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "KILLS_AGENT_PROCESS" in message
        assert "stopped by policy" in message.lower()
        # Category-specific corrective guidance:
        assert "re-declare" in message.lower()

    @pytest.mark.asyncio
    async def test_multi_blocked_feedback_lists_every_category(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS",
            "MODIFIES_PROTECTED_FILES,DELETES_FILES,RUNS_INLINE_CODE",
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello",
            side_effects=[
                "MODIFIES_PROTECTED_FILES",
                "DELETES_FILES",
                "RUNS_INLINE_CODE",
            ],
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "MODIFIES_PROTECTED_FILES" in message
        assert "DELETES_FILES" in message
        assert "RUNS_INLINE_CODE" in message
        # Each category must surface its own guidance line.
        assert "MODIFIES_PROTECTED_FILES:" in message
        assert "DELETES_FILES:" in message
        assert "RUNS_INLINE_CODE:" in message

    @pytest.mark.asyncio
    async def test_generic_category_gets_fallback_guidance(self, monkeypatch):
        """Categories without custom guidance still get a fallback line."""
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "CHANGES_PACKAGES_OR_DEPENDENCIES"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["CHANGES_PACKAGES_OR_DEPENDENCIES"]
        )
        assert result["status"] == "failed_to_start"
        message = result["error"]
        assert "CHANGES_PACKAGES_OR_DEPENDENCIES" in message
        assert "stopped by policy" in message.lower()
        # The fallback generic guidance must be present.
        assert "re-declare" in message.lower() or "operator policy" in message.lower()


class TestConfigEmptyBlockSet:
    @pytest.mark.asyncio
    async def test_unblocking_default_runs_default_blocked_category(self, monkeypatch):
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", ",")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_PROTECTED_FILES"]
        )
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_unblocking_allows_modifies_os_user_settings(self, monkeypatch):
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", ",")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_OS_USER_SETTINGS"]
        )
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_unblocking_allows_kills_agent_process(self, monkeypatch):
        monkeypatch.setenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", ",")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["KILLS_AGENT_PROCESS"]
        )
        assert result["status"] == "completed"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell check")
class TestRejectionOrdering:
    @pytest.mark.asyncio
    async def test_blocked_runs_before_cwd_policy(self, monkeypatch):
        """A blocked side-effect must reject before cwd policy even fires."""
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/allowed")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "MODIFIES_PROTECTED_FILES"
        )
        config = Config()
        mgr = ProcessManager(config)
        # /etc is outside the allowed cwd, so cwd policy would reject this.
        # The side-effect gate should still run first and produce a
        # side-effect rejection.
        result = await mgr.exec_command(
            "echo hello",
            cwd="/etc",
            side_effects=["MODIFIES_PROTECTED_FILES"],
        )
        assert result["status"] == "failed_to_start"
        assert "MODIFIES_PROTECTED_FILES" in result["error"]
        assert "not under allowed roots" not in result["error"]

    @pytest.mark.asyncio
    async def test_blocked_runs_before_command_policy(self, monkeypatch):
        """A blocked side-effect must reject before command regex policy."""
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"echo")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "MODIFIES_PROTECTED_FILES"
        )
        config = Config()
        mgr = ProcessManager(config)
        # The command regex would reject "echo hello", but the side-effect
        # gate should run first.
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_PROTECTED_FILES"]
        )
        assert result["status"] == "failed_to_start"
        assert "MODIFIES_PROTECTED_FILES" in result["error"]
        assert "denied by policy" not in result["error"]

    @pytest.mark.asyncio
    async def test_side_effect_validation_runs_before_process_limit(self, monkeypatch):
        """A blocked side-effect must reject before the process limit check."""
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "1")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "MODIFIES_PROTECTED_FILES"
        )
        config = Config()
        mgr = ProcessManager(config)
        # We don't even start a process — the gate should run first.
        result = await mgr.exec_command(
            "sleep 60", side_effects=["MODIFIES_PROTECTED_FILES"], yield_ms=0
        )
        assert result["status"] == "failed_to_start"
        assert "MODIFIES_PROTECTED_FILES" in result["error"]
        assert "limit" not in result["error"].lower()

    @pytest.mark.asyncio
    async def test_inline_generated_runs_before_cwd_policy(self, monkeypatch):
        """A blocked inline-generated call must reject before cwd policy."""
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/allowed")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "RUNS_INLINE_CODE"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello",
            cwd="/etc",
            side_effects=["RUNS_INLINE_CODE"],
        )
        assert result["status"] == "failed_to_start"
        assert "RUNS_INLINE_CODE" in result["error"]
        assert "not under allowed roots" not in result["error"]

    @pytest.mark.asyncio
    async def test_inline_generated_runs_before_command_policy(self, monkeypatch):
        """A blocked inline-generated call must reject before command policy."""
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"echo")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "RUNS_INLINE_CODE"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["RUNS_INLINE_CODE"]
        )
        assert result["status"] == "failed_to_start"
        assert "RUNS_INLINE_CODE" in result["error"]
        assert "denied by policy" not in result["error"]

    @pytest.mark.asyncio
    async def test_inline_generated_runs_before_process_limit(self, monkeypatch):
        """A blocked inline-generated call must reject before the process limit."""
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "1")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "RUNS_INLINE_CODE"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "sleep 60",
            side_effects=["RUNS_INLINE_CODE"],
            yield_ms=0,
        )
        assert result["status"] == "failed_to_start"
        assert "RUNS_INLINE_CODE" in result["error"]
        assert "limit" not in result["error"].lower()

    @pytest.mark.asyncio
    async def test_modifies_os_user_settings_runs_before_cwd_policy(self, monkeypatch):
        """A blocked MODIFIES_OS_USER_SETTINGS must reject before cwd policy."""
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/allowed")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "MODIFIES_OS_USER_SETTINGS"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello",
            cwd="/etc",
            side_effects=["MODIFIES_OS_USER_SETTINGS"],
        )
        assert result["status"] == "failed_to_start"
        assert "MODIFIES_OS_USER_SETTINGS" in result["error"]
        assert "not under allowed roots" not in result["error"]

    @pytest.mark.asyncio
    async def test_kills_agent_process_runs_before_cwd_policy(self, monkeypatch):
        """A blocked KILLS_AGENT_PROCESS must reject before cwd policy."""
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/allowed")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "KILLS_AGENT_PROCESS"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello",
            cwd="/etc",
            side_effects=["KILLS_AGENT_PROCESS"],
        )
        assert result["status"] == "failed_to_start"
        assert "KILLS_AGENT_PROCESS" in result["error"]
        assert "not under allowed roots" not in result["error"]

    @pytest.mark.asyncio
    async def test_modifies_os_user_settings_runs_before_command_policy(
        self, monkeypatch
    ):
        """A blocked MODIFIES_OS_USER_SETTINGS must reject before command policy."""
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"echo")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "MODIFIES_OS_USER_SETTINGS"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["MODIFIES_OS_USER_SETTINGS"]
        )
        assert result["status"] == "failed_to_start"
        assert "MODIFIES_OS_USER_SETTINGS" in result["error"]
        assert "denied by policy" not in result["error"]

    @pytest.mark.asyncio
    async def test_kills_agent_process_runs_before_command_policy(
        self, monkeypatch
    ):
        """A blocked KILLS_AGENT_PROCESS must reject before command policy."""
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"echo")
        monkeypatch.setenv(
            "MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", "KILLS_AGENT_PROCESS"
        )
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["KILLS_AGENT_PROCESS"]
        )
        assert result["status"] == "failed_to_start"
        assert "KILLS_AGENT_PROCESS" in result["error"]
        assert "denied by policy" not in result["error"]


class TestSideEffectEnumCanonical:
    def test_canonical_names_match_spec(self):
        names = {member.name for member in SideEffect}
        expected = {
            "CHANGES_NETWORK_CONFIGURATION",
            "CHANGES_PACKAGES_OR_DEPENDENCIES",
            "CONSUMES_SIGNIFICANT_RESOURCES",
            "DELETES_FILES",
            "EXPOSES_SECRETS",
            "KILLS_AGENT_PROCESS",
            "MAKES_NETWORK_REQUESTS",
            "MODIFIES_OS_SETTINGS",
            "MODIFIES_OS_USER_SETTINGS",
            "MODIFIES_OUTSIDE_WORKSPACE",
            "MODIFIES_PRODUCTION_SERVICES",
            "MODIFIES_PROTECTED_FILES",
            "MODIFIES_SECURITY_CONTROLS",
            "MODIFIES_WORKSPACE_FILES",
            "NONE",
            "OTHER",
            "RUNS_INLINE_CODE",
            "RUNS_PRIVILEGED_COMMANDS",
            "STOPS_OR_RESTARTS_SERVICES",
            "UNKNOWN",
            "USES_DESTRUCTIVE_GIT_OPERATION",
        }
        assert names == expected

    def test_values_are_uppercase_underscore_strings(self):
        for member in SideEffect:
            assert member.value == member.name
            assert member.value.isupper() or member.value.replace("_", "").isalpha()
            assert " " not in member.value

    def test_runs_inline_code_is_canonical_name(self):
        assert hasattr(SideEffect, "RUNS_INLINE_CODE")
        assert SideEffect.RUNS_INLINE_CODE.value == "RUNS_INLINE_CODE"


class TestExecDocstring:
    def test_docstring_mentions_required_side_effects(self):
        doc = exec.__doc__ or ""
        assert "required" in doc.lower()
        assert "side_effects" in doc

    def test_docstring_states_none_is_exclusive(self):
        doc = exec.__doc__ or ""
        assert "NONE" in doc
        assert "exclusive" in doc.lower()

    def test_docstring_surfaces_inline_code_category(self):
        doc = exec.__doc__ or ""
        assert "RUNS_INLINE_CODE" in doc

    def test_docstring_states_category_in_default_blocklist(self):
        doc = exec.__doc__ or ""
        # The docstring must explicitly say the category is in the default
        # blocklist so agents learn it from the schema alone.
        assert "default blocklist" in doc.lower() or "default" in doc.lower()
        assert "RUNS_INLINE_CODE" in doc
        # Find a sentence that includes the category and a default-blocklist cue.
        lowered = doc.lower()
        idx = lowered.find("runs_inline_code")
        assert idx != -1
        window = lowered[max(0, idx - 200): idx + 200]
        assert "default" in window

    def test_docstring_mentions_modifies_os_user_settings(self):
        doc = exec.__doc__ or ""
        assert "MODIFIES_OS_USER_SETTINGS" in doc

    def test_docstring_mentions_kills_agent_process(self):
        doc = exec.__doc__ or ""
        assert "KILLS_AGENT_PROCESS" in doc

    def test_docstring_includes_safer_next_action_hint(self):
        doc = exec.__doc__ or ""
        lowered = doc.lower()
        # The safer-next-action hint must be discoverable in the docstring.
        assert "reviewable" in lowered
        assert "workspace" in lowered or "file" in lowered

    def test_docstring_includes_examples_for_major_categories(self):
        doc = exec.__doc__ or ""
        for canonical in (
            "NONE",
            "MODIFIES_WORKSPACE_FILES",
            "MODIFIES_PROTECTED_FILES",
            "DELETES_FILES",
            "CHANGES_PACKAGES_OR_DEPENDENCIES",
            "MAKES_NETWORK_REQUESTS",
            "RUNS_PRIVILEGED_COMMANDS",
            "RUNS_INLINE_CODE",
        ):
            assert canonical in doc, f"missing example for {canonical}"


class TestReadmeSideEffectsGuide:
    @pytest.fixture
    def readme_text(self):
        readme_path = (
            Path(__file__).resolve().parent.parent / "README.md"
        )
        return readme_path.read_text(encoding="utf-8")

    def test_readme_lists_all_allowed_enum_values(self, readme_text):
        # The full enum list including the new value must appear in README.
        assert "RUNS_INLINE_CODE" in readme_text

    def test_readme_documents_required_and_exclusive_rules(self, readme_text):
        lowered = readme_text.lower()
        assert "required" in lowered
        assert "side_effects" in lowered
        assert "exclusive" in lowered
        assert "none" in lowered

    def test_readme_lists_examples_for_major_categories(self, readme_text):
        # README must provide example commands for the major categories
        # enumerated in the spec.
        for canonical in (
            "NONE",
            "MODIFIES_WORKSPACE_FILES",
            "MODIFIES_PROTECTED_FILES",
            "DELETES_FILES",
            "CHANGES_PACKAGES_OR_DEPENDENCIES",
            "MAKES_NETWORK_REQUESTS",
            "RUNS_PRIVILEGED_COMMANDS",
        ):
            assert canonical in readme_text, f"README missing example for {canonical}"

    def test_readme_warns_about_inline_code_execution(
        self, readme_text
    ):
        lowered = readme_text.lower()
        assert "inline" in lowered
        assert "reviewable" in lowered
        assert "workspace" in lowered
        # The new category must be in the readme's discussion of inline content.
        assert "RUNS_INLINE_CODE" in readme_text

    def test_readme_default_blocklist_mentions_runs_inline_code(self, readme_text):
        # The expanded default blocklist must be visible in the README
        # (configuration table and/or security section).
        assert "RUNS_INLINE_CODE" in readme_text
        # And the security text must reflect the expanded default.
        assert (
            "MODIFIES_PROTECTED_FILES" in readme_text
            and "MODIFIES_OS_SETTINGS" in readme_text
            and "RUNS_INLINE_CODE" in readme_text
        )

    def test_readme_default_blocklist_mentions_modifies_os_user_settings(
        self, readme_text
    ):
        assert "MODIFIES_OS_USER_SETTINGS" in readme_text

    def test_readme_default_blocklist_mentions_kills_agent_process(
        self, readme_text
    ):
        assert "KILLS_AGENT_PROCESS" in readme_text

    def test_readme_documents_modifies_os_user_settings_scoping(self, readme_text):
        lowered = readme_text.lower()
        assert "user-level" in lowered or "user settings" in lowered
        # Must be distinct from MODIFIES_OS_SETTINGS
        assert "modifies_os_user_settings" in lowered

    def test_readme_documents_kills_agent_process_scoping(self, readme_text):
        lowered = readme_text.lower()
        assert "agent" in lowered or "mcp" in lowered
        assert "kills_agent_process" in lowered

    def test_readme_explains_operator_override(self, readme_text):
        # Operators must be able to clear or override the default blocklist.
        lowered = readme_text.lower()
        assert "mcp_yieldshell_blocked_side_effects" in lowered
        assert "operator" in lowered or "override" in lowered
        # The doc must surface the `,` (empty) override at least once.
        assert "`," in readme_text or "`, `" in readme_text or "set to `,`" in lowered
