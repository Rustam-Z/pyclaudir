"""Auto-discovery: dropping a new file in pyclaudir/tools/ should be enough."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from pyclaudir import tools as tools_pkg
from pyclaudir.mcp_server import discover_tool_classes
from pyclaudir.tools.base import BaseTool, ToolContext


def test_now_tool_is_discovered() -> None:
    classes = discover_tool_classes()
    names = {c.name for c in classes}
    assert "now" in names


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
            from pyclaudir.tools.base import BaseTool, ToolResult

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
        sys.modules.pop("pyclaudir.tools._disco_test_echo", None)
        classes = discover_tool_classes()
        names = {c.name for c in classes}
        assert "_disco_test_echo" in names
    finally:
        new_file.unlink(missing_ok=True)
        sys.modules.pop("pyclaudir.tools._disco_test_echo", None)


@pytest.mark.asyncio
async def test_now_tool_runs() -> None:
    from pyclaudir.tools.now import NowArgs, NowTool

    tool = NowTool(ToolContext())
    result = await tool.run(NowArgs())
    assert "utc=" in result.content
    assert result.is_error is False
