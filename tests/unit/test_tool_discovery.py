"""Auto-discovery: dropping a new file in hamroh/tools/ should be enough."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from hamroh import tools as tools_pkg
from hamroh.cc_worker.event_handlers import USER_VISIBLE_TOOLS
from hamroh.mcp_server import _make_wrapper, discover_tool_classes
from hamroh.tools.base import BaseTool, ToolContext

MCP_PREFIX = "mcp__hamroh__"


def _input_schema(tool: BaseTool) -> dict:
    """Build the JSON input schema FastMCP exposes for a tool — the exact
    ``func_metadata`` path FastMCP uses at registration."""
    wrapper = _make_wrapper(tool, db_logger=None)
    meta = func_metadata(wrapper, structured_output=False)
    return meta.arg_model.model_json_schema()


def test_field_descriptions_and_constraints_reach_the_schema() -> None:
    """The wrapper must carry each pydantic ``Field`` description AND its
    constraints into the MCP input schema — otherwise everything we write in
    ``Field(description=..., max_length=...)`` is invisible to the model."""
    from hamroh.tools.telegram.telegram_create_poll import TelegramCreatePollTool

    props = _input_schema(TelegramCreatePollTool(ToolContext()))["properties"]

    # A per-field description survives the flat-parameter wrapper.
    assert props["correct_option_id"]["description"], (
        "Field descriptions are being dropped before the model sees them"
    )
    # A length constraint survives too.
    assert props["question"]["maxLength"] == 300, (
        "Field constraints are being dropped before the model sees them"
    )


def test_now_tool_is_discovered() -> None:
    classes = discover_tool_classes()
    names = {c.name for c in classes}
    assert "time_now" in names


def test_user_visible_tools_pin_to_real_tools() -> None:
    """``USER_VISIBLE_TOOLS`` is the source of truth for "did the user
    perceive a response" in dropped-text detection. Pin the set, and prove
    every entry maps to a real tool so a rename can't silently weaken
    detection (the one risk of a hand-maintained list)."""
    assert USER_VISIBLE_TOOLS == {
        f"{MCP_PREFIX}telegram_send_message",
        f"{MCP_PREFIX}telegram_reply_to_message",
        f"{MCP_PREFIX}telegram_send_photo",
        f"{MCP_PREFIX}telegram_send_memory_document",
        f"{MCP_PREFIX}telegram_create_poll",
        f"{MCP_PREFIX}telegram_add_reaction",
        f"{MCP_PREFIX}telegram_edit_message",
        f"{MCP_PREFIX}telegram_delete_message",
        f"{MCP_PREFIX}telegram_stop_poll",
    }
    discovered = {c.name for c in discover_tool_classes()}
    for namespaced in USER_VISIBLE_TOOLS:
        bare = namespaced.removeprefix(MCP_PREFIX)
        assert bare in discovered, f"{namespaced} maps to no real tool"


def test_basetool_is_not_itself_returned() -> None:
    classes = discover_tool_classes()
    assert BaseTool not in classes


def test_dropping_a_file_registers_a_new_tool(tmp_path: Path) -> None:
    """Spec line 7: 'New tools are added by dropping a Python file. No core
    code changes required.' We prove that here by writing a fresh tool file
    into the tools/ package directory and asserting it shows up.
    """
    tools_dir = Path(tools_pkg.__file__).parent
    new_file = tools_dir / "_disco_test_echo.py"
    new_file.write_text(
        textwrap.dedent(
            """
            from pydantic import BaseModel
            from hamroh.tools.base import BaseTool, ToolResult

            class EchoArgs(BaseModel):
                text: str

            class DiscoTestEchoTool(BaseTool):
                name = "_disco_test_echo"
                description = "Echo input back. For tests only."
                args_model = EchoArgs

                async def run(self, args):
                    return ToolResult(content=args.text)
            """
        )
    )
    try:
        # Drop any cached import so discover() reloads the package
        sys.modules.pop("hamroh.tools._disco_test_echo", None)
        classes = discover_tool_classes()
        names = {c.name for c in classes}
        assert "_disco_test_echo" in names
    finally:
        new_file.unlink(missing_ok=True)
        sys.modules.pop("hamroh.tools._disco_test_echo", None)


@pytest.mark.asyncio
async def test_now_tool_runs() -> None:
    from hamroh.tools.now import NowArgs, NowTool

    tool = NowTool(ToolContext())
    result = await tool.run(NowArgs())
    assert "utc=" in result.content
    assert result.is_error is False
