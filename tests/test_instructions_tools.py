"""MCP instruction tools — owner-DM gate tests.

Verifies that every one of list/read/write/append returns
``"permission denied"`` when the caller isn't owner-in-DM, and succeeds
when they are.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.instructions_store import InstructionsStore
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.instructions import (
    AppendInstructionsArgs,
    AppendInstructionsTool,
    ListInstructionsArgs,
    ListInstructionsTool,
    ReadInstructionsArgs,
    ReadInstructionsTool,
    WriteInstructionsArgs,
    WriteInstructionsTool,
)


OWNER = 42
STRANGER = 99


def _store(tmp_path: Path) -> InstructionsStore:
    project_root = tmp_path / "proj"
    (project_root / "prompts").mkdir(parents=True)
    (project_root / "prompts" / "system.md").write_text("SYSTEM v1\n")
    (project_root / "prompts" / "project.md").write_text("PROJECT v1\n")
    s = InstructionsStore(project_root=project_root, backup_dir=tmp_path / "backups")
    s.ensure_dirs()
    return s


def _ctx(store: InstructionsStore, *, user_id: int | None, chat_type: str | None) -> ToolContext:
    return ToolContext(
        instructions_store=store,
        owner_id=OWNER,
        last_inbound_user_id=user_id,
        last_inbound_chat_type=chat_type,
    )


# ---------------------------------------------------------------------------
# Denial path — every tool, every wrong context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_cls,args",
    [
        (ListInstructionsTool, ListInstructionsArgs()),
        (ReadInstructionsTool, ReadInstructionsArgs(name="system")),
        (WriteInstructionsTool, WriteInstructionsArgs(name="system", content="x")),
        (AppendInstructionsTool, AppendInstructionsArgs(name="system", content="x")),
    ],
)
async def test_denied_when_not_owner(tmp_path: Path, tool_cls, args) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=STRANGER, chat_type="private")
    result = await tool_cls(ctx).run(args)
    assert result.is_error is True
    assert result.content == "permission denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_cls,args",
    [
        (ListInstructionsTool, ListInstructionsArgs()),
        (ReadInstructionsTool, ReadInstructionsArgs(name="system")),
        (WriteInstructionsTool, WriteInstructionsArgs(name="system", content="x")),
        (AppendInstructionsTool, AppendInstructionsArgs(name="system", content="x")),
    ],
)
async def test_denied_when_group_even_as_owner(tmp_path: Path, tool_cls, args) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=OWNER, chat_type="supergroup")
    result = await tool_cls(ctx).run(args)
    assert result.is_error is True
    assert result.content == "permission denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_cls,args",
    [
        (ListInstructionsTool, ListInstructionsArgs()),
        (ReadInstructionsTool, ReadInstructionsArgs(name="system")),
        (WriteInstructionsTool, WriteInstructionsArgs(name="system", content="x")),
        (AppendInstructionsTool, AppendInstructionsArgs(name="system", content="x")),
    ],
)
async def test_denied_when_owner_id_not_configured(tmp_path: Path, tool_cls, args) -> None:
    store = _store(tmp_path)
    ctx = ToolContext(
        instructions_store=store,
        owner_id=None,  # not configured
        last_inbound_user_id=OWNER,
        last_inbound_chat_type="private",
    )
    result = await tool_cls(ctx).run(args)
    assert result.is_error is True
    assert result.content == "permission denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_cls,args",
    [
        (ListInstructionsTool, ListInstructionsArgs()),
        (ReadInstructionsTool, ReadInstructionsArgs(name="system")),
        (WriteInstructionsTool, WriteInstructionsArgs(name="system", content="x")),
        (AppendInstructionsTool, AppendInstructionsArgs(name="system", content="x")),
    ],
)
async def test_denied_before_any_request_received(tmp_path: Path, tool_cls, args) -> None:
    """Initial state at boot: last_inbound_* is None. All calls denied."""
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=None, chat_type=None)
    result = await tool_cls(ctx).run(args)
    assert result.is_error is True
    assert result.content == "permission denied"


# ---------------------------------------------------------------------------
# Success path — owner in DM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_in_dm_can_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=OWNER, chat_type="private")
    result = await ListInstructionsTool(ctx).run(ListInstructionsArgs())
    assert result.is_error is False
    assert "system" in result.content
    assert "project" in result.content


@pytest.mark.asyncio
async def test_owner_in_dm_can_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=OWNER, chat_type="private")
    result = await ReadInstructionsTool(ctx).run(ReadInstructionsArgs(name="system"))
    assert result.is_error is False
    assert "SYSTEM v1" in result.content


@pytest.mark.asyncio
async def test_owner_in_dm_can_write_after_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=OWNER, chat_type="private")
    # read first (required by read-before-write rail)
    await ReadInstructionsTool(ctx).run(ReadInstructionsArgs(name="project"))
    # now write succeeds
    result = await WriteInstructionsTool(ctx).run(
        WriteInstructionsArgs(name="project", content="PROJECT v2\n")
    )
    assert result.is_error is False
    assert "wrote" in result.content
    assert "project.md" in result.content
    # file on disk has new content
    assert (store.root / "prompts" / "project.md").read_text() == "PROJECT v2\n"


@pytest.mark.asyncio
async def test_write_without_prior_read_fails_even_for_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=OWNER, chat_type="private")
    # skip the read — should be rejected at the store layer, not the gate
    result = await WriteInstructionsTool(ctx).run(
        WriteInstructionsArgs(name="project", content="PROJECT v2\n")
    )
    assert result.is_error is True
    assert "read-before-write" in result.content


@pytest.mark.asyncio
async def test_owner_in_dm_can_append_after_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=OWNER, chat_type="private")
    await ReadInstructionsTool(ctx).run(ReadInstructionsArgs(name="project"))
    result = await AppendInstructionsTool(ctx).run(
        AppendInstructionsArgs(name="project", content="extra line\n")
    )
    assert result.is_error is False
    assert (store.root / "prompts" / "project.md").read_text() == "PROJECT v1\nextra line\n"


@pytest.mark.asyncio
async def test_write_creates_backup_visible_in_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, user_id=OWNER, chat_type="private")
    await ReadInstructionsTool(ctx).run(ReadInstructionsArgs(name="system"))
    result = await WriteInstructionsTool(ctx).run(
        WriteInstructionsArgs(name="system", content="SYSTEM v2\n")
    )
    assert result.data is not None
    backup_path = result.data.get("backup")
    assert backup_path is not None
    # The backup file exists with the pre-edit content.
    assert Path(backup_path).read_text() == "SYSTEM v1\n"
