"""Tests for the required side-effect declaration on ``exec_command``."""

from __future__ import annotations

import json
import sys

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
            "NONE",
            "MODIFIES_WORKSPACE_FILES",
            "MODIFIES_PROTECTED_FILES",
            "MODIFIES_OUTSIDE_WORKSPACE",
            "DELETES_FILES",
            "INSTALLS_DEPENDENCIES",
            "CHANGES_SYSTEM_CONFIGURATION",
            "BREAKS_OPERATING_SYSTEM",
            "AFFECTS_PRODUCTION_SERVICES",
            "STOPS_OR_RESTARTS_SERVICES",
            "EXPOSES_SECRETS",
            "CREATES_SECURITY_RISK",
            "CHANGES_NETWORK_CONFIGURATION",
            "MAKES_NETWORK_REQUESTS",
            "RUNS_PRIVILEGED_COMMANDS",
            "USES_DESTRUCTIVE_GIT_OPERATION",
            "CONSUMES_SIGNIFICANT_RESOURCES",
            "OTHER",
            "UNKNOWN",
        ):
            assert canonical in enum_values

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
    async def test_blocked_breaks_operating_system_rejected(self, monkeypatch):
        monkeypatch.delenv("MCP_YIELDSHELL_BLOCKED_SIDE_EFFECTS", raising=False)
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            "echo hello", side_effects=["BREAKS_OPERATING_SYSTEM"]
        )
        assert result["status"] == "failed_to_start"
        assert "BREAKS_OPERATING_SYSTEM" in result["error"]

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


class TestSideEffectEnumCanonical:
    def test_canonical_names_match_spec(self):
        names = {member.name for member in SideEffect}
        expected = {
            "NONE",
            "MODIFIES_WORKSPACE_FILES",
            "MODIFIES_PROTECTED_FILES",
            "MODIFIES_OUTSIDE_WORKSPACE",
            "DELETES_FILES",
            "INSTALLS_DEPENDENCIES",
            "CHANGES_SYSTEM_CONFIGURATION",
            "BREAKS_OPERATING_SYSTEM",
            "AFFECTS_PRODUCTION_SERVICES",
            "STOPS_OR_RESTARTS_SERVICES",
            "EXPOSES_SECRETS",
            "CREATES_SECURITY_RISK",
            "CHANGES_NETWORK_CONFIGURATION",
            "MAKES_NETWORK_REQUESTS",
            "RUNS_PRIVILEGED_COMMANDS",
            "USES_DESTRUCTIVE_GIT_OPERATION",
            "CONSUMES_SIGNIFICANT_RESOURCES",
            "OTHER",
            "UNKNOWN",
        }
        assert names == expected

    def test_values_are_uppercase_underscore_strings(self):
        for member in SideEffect:
            assert member.value == member.name
            assert member.value.isupper() or member.value.replace("_", "").isalpha()
            assert " " not in member.value
