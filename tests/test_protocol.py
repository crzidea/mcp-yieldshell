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
                    "exec",
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

            # No since_seq anywhere: the server-side cursor is what keeps
            # these responses from repeating output.
            collected = started["stdout"]
            pages = 0
            for _ in range(20):
                page = _payload(
                    await session.call_tool(
                        "wait",
                        {
                            "process_id": started["process_id"],
                            "timeout_ms": 400,
                        },
                    )
                )
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
                    {"process_id": started["process_id"], "tail_lines": 2},
                )
            )
            assert tail["stdout"] == "chunk-5\nchunk-6\n"

            listing = _payload(await session.call_tool("ps", {}))
            entry = listing["processes"][0]
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
