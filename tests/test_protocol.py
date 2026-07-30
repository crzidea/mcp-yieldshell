"""End-to-end checks against the packaged stdio MCP server."""

from __future__ import annotations

import json
import re
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent


def _payload(result: CallToolResult) -> dict:
    assert result.isError is False
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    return json.loads(result.content[0].text)


def _assert_bare_execution_id(value: object) -> str:
    assert isinstance(value, str)
    assert re.fullmatch(r"[0-9a-f]{12}", value)
    return value


@pytest.mark.asyncio
async def test_stdio_server_lists_and_executes_tools():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_yieldshell"],
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            tools_by_name = {tool.name: tool for tool in tools.tools}
            assert tool_names == {
                "cleanup",
                "execute",
                "ps",
                "read",
                "stop",
                "wait",
                "write",
            }
            assert "exec" not in tool_names
            for tool_name in ("read", "write", "wait", "stop"):
                tool = tools_by_name[tool_name]
                properties = tool.inputSchema["properties"]
                assert "execution_id" in properties
                assert properties["execution_id"]["type"] == "string"
                assert "execution_id" in tool.inputSchema["required"]
                assert "process_id" not in properties
                assert "opaque" in (tool.description or "")
                assert "passed through unchanged" in (tool.description or "")

            result = await session.call_tool(
                "execute",
                {
                    "command": "printf protocol-ok",
                    "side_effects": ["NONE"],
                },
            )

            payload = _payload(result)
            assert payload["status"] == "completed"
            assert payload["stdout"] == "protocol-ok"
            assert isinstance(payload["execution_id"], str)
            assert isinstance(payload["process_id"], int)
            assert "pid" not in payload

            rejected = await session.call_tool(
                "read",
                {"process_id": str(payload["process_id"])},
            )
            assert rejected.isError is True


@pytest.mark.asyncio
async def test_stdio_server_incremental_polling_does_not_replay_output():
    """Poll a chatty process over the real transport without duplication."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_yieldshell"],
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            started = _payload(
                await session.call_tool(
                    "execute",
                    {
                        "command": (
                            "for i in 1 2 3 4 5 6; do echo chunk-$i; "
                            "sleep 0.2; done"
                        ),
                        "side_effects": ["NONE"],
                        "yield_ms": 0,
                    },
                )
            )
            assert started["status"] == "backgrounded"
            execution_id = _assert_bare_execution_id(started["execution_id"])
            assert isinstance(started["process_id"], int)
            assert execution_id != str(started["process_id"])

            # No since_seq anywhere: the server-side cursor is what keeps
            # these responses from repeating output.
            collected = started["stdout"]
            pages = 0
            for _ in range(20):
                page = _payload(
                    await session.call_tool(
                        "wait",
                        {
                            "execution_id": execution_id,
                            "timeout_ms": 400,
                        },
                    )
                )
                assert _assert_bare_execution_id(page["execution_id"]) == execution_id
                pages += 1
                collected += page["stdout"]
                assert page["wait_result"] in ("exited", "deadline_reached")
                assert page["max_wait_ms"] == 400
                if page["wait_result"] == "exited":
                    break

            expected = "".join(f"chunk-{index}\n" for index in range(1, 7))
            assert collected == expected
            assert pages > 1

            tail = _payload(
                await session.call_tool(
                    "read",
                    {"execution_id": execution_id, "tail_lines": 2},
                )
            )
            assert _assert_bare_execution_id(tail["execution_id"]) == execution_id
            assert tail["stdout"] == "chunk-5\nchunk-6\n"

            listing = _payload(await session.call_tool("ps", {}))
            entry = listing["executions"][0]
            assert _assert_bare_execution_id(entry["execution_id"]) == execution_id
            assert entry["process_id"] == started["process_id"]
            assert entry["execution_id"] != str(entry["process_id"])
            assert entry["idle_ms"] >= 0
            assert entry["latest_seq"] == len(expected) + 1


@pytest.mark.asyncio
async def test_stdio_server_background_input_wait_stop_and_cleanup_workflow():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_yieldshell"],
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            started = _payload(
                await session.call_tool(
                    "execute",
                    {
                        "command": "cat",
                        "side_effects": ["NONE"],
                        "close_stdin": False,
                        "yield_ms": 0,
                    },
                )
            )
            assert started["status"] == "backgrounded"
            execution_id = _assert_bare_execution_id(started["execution_id"])

            written = _payload(
                await session.call_tool(
                    "write",
                    {
                        "execution_id": execution_id,
                        "input": "protocol-input",
                        "newline": True,
                        "close_stdin": True,
                    },
                )
            )
            assert written["ok"] is True
            assert _assert_bare_execution_id(written["execution_id"]) == execution_id

            waited = _payload(
                await session.call_tool(
                    "wait",
                    {
                        "execution_id": execution_id,
                        "timeout_ms": 5_000,
                    },
                )
            )
            assert waited["status"] == "completed"
            assert _assert_bare_execution_id(waited["execution_id"]) == execution_id
            assert waited["stdout"] == "protocol-input\n"

            sleeper = _payload(
                await session.call_tool(
                    "execute",
                    {
                        "command": "sleep 30",
                        "side_effects": ["NONE"],
                        "yield_ms": 0,
                    },
                )
            )
            assert sleeper["status"] == "backgrounded"
            sleeper_id = _assert_bare_execution_id(sleeper["execution_id"])

            stopped = _payload(
                await session.call_tool(
                    "stop",
                    {
                        "execution_id": sleeper_id,
                        "force_after_ms": 100,
                    },
                )
            )
            assert stopped["stopped"] is True
            assert _assert_bare_execution_id(stopped["execution_id"]) == sleeper_id

            cleaned = _payload(
                await session.call_tool(
                    "cleanup",
                    {
                        "completed_older_than_ms": 0,
                        "stopped_older_than_ms": 0,
                    },
                )
            )
            assert cleaned["removed"] == 2

            listing = _payload(await session.call_tool("ps", {}))
            assert listing["executions"] == []
