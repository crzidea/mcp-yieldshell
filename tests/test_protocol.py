"""End-to-end checks against the packaged stdio MCP server."""

from __future__ import annotations

import json
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
            assert {tool.name for tool in tools.tools} == {
                "cleanup",
                "exec",
                "ps",
                "read",
                "stop",
                "wait",
                "write",
            }

            result = await session.call_tool(
                "exec",
                {
                    "command": "printf protocol-ok",
                    "side_effects": ["NONE"],
                },
            )

            payload = _payload(result)
            assert payload["status"] == "completed"
            assert payload["stdout"] == "protocol-ok"


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
                    "exec",
                    {
                        "command": "cat",
                        "side_effects": ["NONE"],
                        "close_stdin": False,
                        "yield_ms": 0,
                    },
                )
            )
            assert started["status"] == "backgrounded"

            written = _payload(
                await session.call_tool(
                    "write",
                    {
                        "process_id": started["process_id"],
                        "input": "protocol-input",
                        "newline": True,
                        "close_stdin": True,
                    },
                )
            )
            assert written["ok"] is True

            waited = _payload(
                await session.call_tool(
                    "wait",
                    {
                        "process_id": started["process_id"],
                        "timeout_ms": 5_000,
                    },
                )
            )
            assert waited["status"] == "completed"
            assert waited["stdout"] == "protocol-input\n"

            sleeper = _payload(
                await session.call_tool(
                    "exec",
                    {
                        "command": "sleep 30",
                        "side_effects": ["NONE"],
                        "yield_ms": 0,
                    },
                )
            )
            assert sleeper["status"] == "backgrounded"

            stopped = _payload(
                await session.call_tool(
                    "stop",
                    {
                        "process_id": sleeper["process_id"],
                        "force_after_ms": 100,
                    },
                )
            )
            assert stopped["stopped"] is True

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
            assert listing["processes"] == []
