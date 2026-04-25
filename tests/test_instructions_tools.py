"""MCP instruction tools — read + append project.md.

The owner-only policy lives in the system prompt; these tests just
cover the tool wrappers' happy path and basic error surfacing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.instructions_store import InstructionsStore
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.instructions import (
    AppendInstructionsArgs,
    AppendInstructionsTool,
    ReadInstructionsArgs,
    ReadInstructionsTool,
)


def _store(tmp_path: Path) -> InstructionsStore:
    project_md = tmp_path / "prompts" / "project.md"
    project_md.parent.mkdir(parents=True)
    project_md.write_text("PROJECT v1\n")
    s = InstructionsStore(project_md_path=project_md, backup_dir=tmp_path / "backups")
    s.ensure_dirs()
    return s


def _ctx(store: InstructionsStore) -> ToolContext:
    return ToolContext(instructions_store=store)


@pytest.mark.asyncio
async def test_read_returns_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = await ReadInstructionsTool(_ctx(store)).run(ReadInstructionsArgs())
    assert result.is_error is False
    assert "PROJECT v1" in result.content


@pytest.mark.asyncio
async def test_append_writes_file_and_returns_backup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = await AppendInstructionsTool(_ctx(store)).run(
        AppendInstructionsArgs(content="extra line\n")
    )
    assert result.is_error is False
    assert store.path.read_text() == "PROJECT v1\nextra line\n"
    assert result.data is not None
    backup_path = result.data.get("backup")
    assert backup_path is not None
    assert Path(backup_path).read_text() == "PROJECT v1\n"


@pytest.mark.asyncio
async def test_read_surfaces_missing_file_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.unlink()
    result = await ReadInstructionsTool(_ctx(store)).run(ReadInstructionsArgs())
    assert result.is_error is True
    assert "not present" in result.content
