"""MCP tool_list tool — enumerates the enabled tools, hides disabled ones."""

from __future__ import annotations

import pytest

from hamroh.mcp_server import build_fastmcp
from hamroh.tools.base import BaseTool, ToolContext, ToolResult
from hamroh.tools.tools import ToolListArgs, ToolListTool


class _FakeTool(BaseTool[ToolListArgs]):
    """A throwaway tool so the listing test doesn't depend on real tools."""

    name = "fake_tool"
    description = "A fake tool used only by the tool_list unit test."
    args_model = ToolListArgs

    async def run(self, args: ToolListArgs) -> ToolResult:  # pragma: no cover
        return ToolResult(content="fake")


@pytest.mark.asyncio
async def test_tool_list_returns_names_and_descriptions() -> None:
    # given a context whose enabled tools were recorded by the server
    ctx = ToolContext(enabled_tools=[_FakeTool(ToolContext())])

    # when the bot asks what tools it has
    result = await ToolListTool(ctx).run(ToolListArgs())

    # then it sees each tool's namespaced (mcp__hamroh__) name and description
    assert result.is_error is False, result.content
    assert "mcp__hamroh__fake_tool" in result.content
    assert "used only by the tool_list unit test" in result.content
    assert result.data is not None
    assert result.data["tools"] == [
        {
            "name": "mcp__hamroh__fake_tool",
            "description": "A fake tool used only by the tool_list unit test.",
        },
    ]


@pytest.mark.asyncio
async def test_tool_list_empty_when_nothing_enabled() -> None:
    # given a context with no enabled tools recorded (e.g. constructed directly)
    ctx = ToolContext()

    # when the bot asks what tools it has
    result = await ToolListTool(ctx).run(ToolListArgs())

    # then it gets the empty marker, not an error
    assert result.is_error is False
    assert result.content == "(no tools)"


@pytest.mark.asyncio
async def test_tool_list_excludes_disabled_tools() -> None:
    # given a server built with one real tool disabled
    ctx = ToolContext()
    build_fastmcp(ctx, disabled=frozenset({"time_now"}))

    # when the bot lists its tools
    result = await ToolListTool(ctx).run(ToolListArgs())

    # then the disabled tool is absent while tool_list itself is present,
    # both under their namespaced mcp__hamroh__ names
    names = {tool["name"] for tool in result.data["tools"]}
    assert "mcp__hamroh__tool_list" in names, "tool_list should list itself"
    assert "mcp__hamroh__time_now" not in names, "disabled tools must not be listed"
