"""MCP schema and validation checks for the required execution description."""

from unittest.mock import AsyncMock, Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.base import Tool

from mcp_yieldshell import server


@pytest.fixture
def execute_tool():
    return Tool.from_function(server.execute)


def test_description_schema_is_required_and_explains_its_purpose(execute_tool):
    schema = execute_tool.parameters
    assert "description" in schema["required"]
    description = schema["properties"]["description"]
    assert description["type"] == "string"
    assert description["minLength"] == 1
    assert description["pattern"] == r"\S"
    assert "what the command does" in description["description"]
    assert "default" not in description
    assert "maxLength" not in description


@pytest.mark.parametrize(
    "description_args",
    [
        pytest.param({}, id="missing"),
        pytest.param({"description": None}, id="null"),
        pytest.param({"description": 123}, id="integer"),
        pytest.param({"description": 1.5}, id="float"),
        pytest.param({"description": True}, id="boolean"),
        pytest.param({"description": []}, id="array"),
        pytest.param({"description": {}}, id="object"),
        pytest.param({"description": ""}, id="empty"),
        pytest.param({"description": " \t\r\n"}, id="whitespace"),
        pytest.param({"description": "\u2003\u00a0"}, id="unicode-whitespace"),
    ],
)
async def test_invalid_description_never_reaches_manager(
    execute_tool, monkeypatch, description_args
):
    get_manager = Mock()
    monkeypatch.setattr(server, "_get_manager", get_manager)

    with pytest.raises(ToolError, match="description"):
        await execute_tool.run(
            {"command": "echo hello", "side_effects": ["NONE"], **description_args}
        )

    get_manager.assert_not_called()


@pytest.mark.parametrize(
    "description",
    [
        "Print a greeting",
        " \n输出问候语\t ",
        "Explain the command. " * 1_000,
    ],
    ids=["concise", "unicode-with-whitespace", "long"],
)
async def test_valid_description_is_not_forwarded_to_manager(
    execute_tool, monkeypatch, description
):
    manager = Mock()
    response = {"status": "completed", "stdout": "hello\n"}
    manager.execute_command = AsyncMock(return_value=response)
    monkeypatch.setattr(server, "_get_manager", lambda: manager)

    result = await execute_tool.run(
        {
            "command": "echo hello",
            "side_effects": ["NONE"],
            "description": description,
            "name": "greeting",
        }
    )

    assert result == response
    manager.execute_command.assert_awaited_once()
    arguments = manager.execute_command.await_args.kwargs
    assert arguments["command"] == "echo hello"
    assert arguments["name"] == "greeting"
    assert "description" not in arguments
