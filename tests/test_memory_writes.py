"""Memory write/append: path safety, size cap, read-before-write rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.storage.memory import MAX_MEMORY_BYTES, MemoryPathError, MemoryStore
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.memory import (
    AppendMemoryArgs,
    AppendMemoryTool,
    ListMemoriesArgs,
    ListMemoriesTool,
    ReadMemoryArgs,
    ReadMemoryTool,
    WriteMemoryArgs,
    WriteMemoryTool,
)


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path / "memories")
    s.ensure_root()
    return s


# ---------------------------------------------------------------------------
# write — happy path
# ---------------------------------------------------------------------------


def test_write_creates_new_file(store: MemoryStore) -> None:
    n = store.write("notes/users/alice.md", "Alice loves cats")
    assert n == len("Alice loves cats")
    assert (store.root / "notes" / "users" / "alice.md").read_text() == "Alice loves cats"


def test_write_creates_parent_dirs(store: MemoryStore) -> None:
    store.write("a/b/c/deep.md", "x")
    assert (store.root / "a" / "b" / "c" / "deep.md").exists()


# ---------------------------------------------------------------------------
# read-before-write enforcement
# ---------------------------------------------------------------------------


def test_overwrite_without_read_is_rejected(store: MemoryStore) -> None:
    """Pre-existing operator notes must not be silently destroyed."""
    operator_note = store.root / "policy.md"
    operator_note.write_text("CRITICAL: do not delete this")
    with pytest.raises(MemoryPathError, match="read-before-write"):
        store.write("policy.md", "lol replaced")
    # Original content untouched
    assert operator_note.read_text() == "CRITICAL: do not delete this"


def test_overwrite_after_read_is_allowed(store: MemoryStore) -> None:
    operator_note = store.root / "policy.md"
    operator_note.write_text("v1")
    assert "v1" in store.read("policy.md")
    store.write("policy.md", "v2")
    assert operator_note.read_text() == "v2"


def test_second_overwrite_in_same_session_does_not_need_reread(store: MemoryStore) -> None:
    """Once we've written it we know its content; further writes are fine."""
    store.write("journal.md", "entry 1")
    store.write("journal.md", "entry 2")  # we wrote it, so the read flag is set
    assert (store.root / "journal.md").read_text() == "entry 2"


def test_read_paths_resets_on_new_store_instance(tmp_path: Path) -> None:
    """Process restart = empty read_paths set, so a new instance must
    re-read before overwriting."""
    root = tmp_path / "memories"
    root.mkdir()

    s1 = MemoryStore(root)
    s1.write("doc.md", "first run wrote this")
    assert s1.read_paths_snapshot == {"doc.md"}

    s2 = MemoryStore(root)
    assert s2.read_paths_snapshot == frozenset()
    with pytest.raises(MemoryPathError, match="read-before-write"):
        s2.write("doc.md", "second run blindly overwrites")


def test_read_records_in_read_paths(store: MemoryStore) -> None:
    (store.root / "x.md").write_text("hi")
    assert "x.md" not in store.read_paths_snapshot
    store.read("x.md")
    assert "x.md" in store.read_paths_snapshot


def test_read_failure_does_not_credit_read_paths(store: MemoryStore) -> None:
    """If the read raises (file doesn't exist), don't unlock writes."""
    with pytest.raises(MemoryPathError):
        store.read("does_not_exist.md")
    assert "does_not_exist.md" not in store.read_paths_snapshot


# ---------------------------------------------------------------------------
# size cap
# ---------------------------------------------------------------------------


def test_write_rejects_too_large(store: MemoryStore) -> None:
    big = "x" * (MAX_MEMORY_BYTES + 1)
    with pytest.raises(MemoryPathError, match="too large"):
        store.write("huge.md", big)
    assert not (store.root / "huge.md").exists()


def test_write_accepts_exactly_at_cap(store: MemoryStore) -> None:
    at_cap = "x" * MAX_MEMORY_BYTES
    n = store.write("ceiling.md", at_cap)
    assert n == MAX_MEMORY_BYTES


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


def test_append_to_new_file(store: MemoryStore) -> None:
    n = store.append("journal.md", "first line\n")
    assert n == len("first line\n")


def test_append_after_read(store: MemoryStore) -> None:
    (store.root / "journal.md").write_text("entry 1\n")
    store.read("journal.md")
    new_size = store.append("journal.md", "entry 2\n")
    assert new_size == len("entry 1\nentry 2\n")
    assert (store.root / "journal.md").read_text() == "entry 1\nentry 2\n"


def test_append_without_read_is_rejected(store: MemoryStore) -> None:
    (store.root / "journal.md").write_text("existing")
    with pytest.raises(MemoryPathError, match="read-before-write"):
        store.append("journal.md", "more")


def test_append_respects_total_cap(store: MemoryStore) -> None:
    near_cap = "x" * (MAX_MEMORY_BYTES - 10)
    store.write("file.md", near_cap)
    with pytest.raises(MemoryPathError, match="cap"):
        store.append("file.md", "y" * 100)


# ---------------------------------------------------------------------------
# path safety still applies to writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "data/memories/../../../etc/passwd",
        "",
    ],
)
def test_write_rejects_hostile_paths(store: MemoryStore, hostile: str) -> None:
    with pytest.raises(MemoryPathError):
        store.write(hostile, "pwned")


def test_write_rejects_symlinked_target(store: MemoryStore, tmp_path: Path) -> None:
    import os

    outside = tmp_path / "secret.txt"
    outside.write_text("operator data")
    link = store.root / "shortcut.md"
    os.symlink(outside, link)
    with pytest.raises(MemoryPathError):
        store.write("shortcut.md", "pwned")
    # The real file outside is untouched
    assert outside.read_text() == "operator data"


# ---------------------------------------------------------------------------
# Tool wrappers (the actual MCP-facing surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_memory_tool_happy_path(store: MemoryStore) -> None:
    ctx = ToolContext(memory_store=store)
    tool = WriteMemoryTool(ctx)
    result = await tool.run(WriteMemoryArgs(path="notes/test.md", content="hi"))
    assert result.is_error is False
    assert "wrote" in result.content
    assert result.data == {"path": "notes/test.md", "bytes": 2}


@pytest.mark.asyncio
async def test_write_memory_tool_returns_error_on_overwrite_without_read(
    store: MemoryStore,
) -> None:
    (store.root / "policy.md").write_text("careful")
    tool = WriteMemoryTool(ToolContext(memory_store=store))
    result = await tool.run(WriteMemoryArgs(path="policy.md", content="oops"))
    assert result.is_error is True
    assert "read-before-write" in result.content


@pytest.mark.asyncio
async def test_full_round_trip_via_tools(store: MemoryStore) -> None:
    """Read → write → read → append using only the tool interface."""
    (store.root / "diary.md").write_text("Day 1: hello\n")
    ctx = ToolContext(memory_store=store)
    read = ReadMemoryTool(ctx)
    write = WriteMemoryTool(ctx)
    append = AppendMemoryTool(ctx)
    list_tool = ListMemoriesTool(ctx)

    # 1. List shows the file
    listing = await list_tool.run(ListMemoriesArgs())
    assert "diary.md" in listing.content

    # 2. Read the file (this unlocks writes/appends to it)
    r1 = await read.run(ReadMemoryArgs(path="diary.md"))
    assert "Day 1" in r1.content

    # 3. Append to it
    a = await append.run(AppendMemoryArgs(path="diary.md", content="Day 2: more\n"))
    assert a.is_error is False
    assert "Day 2" in (store.root / "diary.md").read_text()

    # 4. Overwrite (allowed because we read it earlier this session)
    w = await write.run(WriteMemoryArgs(path="diary.md", content="reset"))
    assert w.is_error is False
    assert (store.root / "diary.md").read_text() == "reset"


@pytest.mark.asyncio
async def test_write_memory_tool_creates_new_file_without_read(
    store: MemoryStore,
) -> None:
    """A brand-new file is allowed without a prior read, since there's
    nothing to lose."""
    tool = WriteMemoryTool(ToolContext(memory_store=store))
    result = await tool.run(WriteMemoryArgs(path="brand_new.md", content="fresh"))
    assert result.is_error is False
    assert (store.root / "brand_new.md").read_text() == "fresh"
