"""The MCP layer is thin by design; verify the contract it exposes."""

import asyncio

from backburner.server import mcp

EXPECTED_TOOLS = {
    "start_task", "task_status", "task_result", "cancel_task", "list_tasks",
}


def test_all_tools_registered():
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_every_tool_has_a_description():
    tools = asyncio.run(mcp.list_tools())
    for tool in tools:
        assert tool.description and len(tool.description) > 20, tool.name


def test_start_task_accepts_timeout_parameter():
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    props = tools["start_task"].input_schema["properties"]
    assert "timeout_seconds" in props
    assert "command" in props
    assert "cwd" in props
