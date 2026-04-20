"""End-to-end check that the MCP server starts on a random port and the
``now`` tool is reachable over HTTP via the official MCP client."""

from __future__ import annotations

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from pyclaudir.mcp_server import McpServer
from pyclaudir.tools.base import ToolContext


@pytest.mark.asyncio
async def test_now_tool_reachable_via_http() -> None:
    server = McpServer(ToolContext())
    await server.start()
    try:
        assert server.port is not None and server.port > 0
        async with streamable_http_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "now" in names

                result = await session.call_tool("now", {})
                assert result.isError in (False, None)
                # FastMCP wraps the return string in a TextContent block.
                texts = [c.text for c in result.content if hasattr(c, "text")]
                assert texts and "utc=" in texts[0]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_mcp_config_file_round_trips() -> None:
    server = McpServer(ToolContext())
    await server.start()
    try:
        path = server.write_mcp_config()
        text = path.read_text()
        assert "pyclaudir" in text
        assert f":{server.port}/mcp" in text
    finally:
        await server.stop()
