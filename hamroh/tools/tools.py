"""tool_list — enumerate the tools currently available to the bot.

Mirrors ``skill_list``/``memory_list``: in-conversation introspection so the
bot can answer "what can I do?" or find tools relevant to a task before acting.
The enabled tools (after the ``disabled`` filter) are recorded on the shared
:class:`ToolContext` by ``build_fastmcp`` — this tool just reads that list, so
disabled tools never show up.
"""

from __future__ import annotations

from pydantic import BaseModel

from .base import BaseTool, ToolResult

#: Claude Code namespaces every hamroh MCP tool as ``mcp__<server>__<name>``;
#: that prefixed form is the exact string Claude calls. We list it (not the
#: bare ``tool.name``) so the names shown are the real callable ones. Mirrors
#: ``_MCP_PREFIX`` in cc_worker/event_handlers.py (server name "hamroh").
_MCP_PREFIX = "mcp__hamroh__"


class ToolListArgs(BaseModel):
    pass


class ToolListTool(BaseTool[ToolListArgs]):
    name = "tool_list"
    description = (
        "List the tools currently available to you, with each tool's name "
        "and short description. Use it to discover what you can do, or to "
        "find tools related to a task before acting. Disabled tools are not "
        "listed."
    )
    args_model = ToolListArgs

    async def run(self, args: ToolListArgs) -> ToolResult:
        tools = sorted(self.ctx.enabled_tools, key=lambda tool: tool.name)
        if not tools:
            return ToolResult(content="(no tools)")
        listed = [
            {"name": f"{_MCP_PREFIX}{tool.name}", "description": tool.description}
            for tool in tools
        ]
        lines = [f"- **{item['name']}** — {item['description']}" for item in listed]
        return ToolResult(content="\n".join(lines), data={"tools": listed})
