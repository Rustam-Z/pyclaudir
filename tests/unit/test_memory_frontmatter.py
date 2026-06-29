"""Memory frontmatter protocol: template enforcement + description surfacing.

Memory files now follow the same name/description frontmatter protocol as
skills: ``memory_write`` rejects content without it, ``memory_list`` surfaces
the description, and ``memory_append`` refreshes the description on every call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hamroh.storage.frontmatter import DESCRIPTION_MAX
from hamroh.storage.memory import MemoryPathError, MemoryStore
from hamroh.tools.base import ToolContext
from hamroh.tools.memory import ListMemoriesArgs, ListMemoriesTool


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path / "memories")
    s.ensure_root()
    return s


def mem(body: str = "", *, name: str = "note", desc: str = "a test memory") -> str:
    """A minimal templated memory file: frontmatter + body."""
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


# ---------------------------------------------------------------------------
# write — the template is mandatory
# ---------------------------------------------------------------------------


def test_write_rejects_missing_frontmatter(store: MemoryStore) -> None:
    """given bare content, when written, then it is refused before touching disk."""
    with pytest.raises(MemoryPathError, match="frontmatter"):
        store.write("n.md", "no frontmatter here")
    assert not (store.root / "n.md").exists(), "rejected write must not create the file"


def test_write_rejects_missing_name(store: MemoryStore) -> None:
    content = "---\ndescription: only a description\n---\n\nbody"
    with pytest.raises(MemoryPathError, match="name"):
        store.write("n.md", content)


def test_write_rejects_missing_description(store: MemoryStore) -> None:
    content = "---\nname: just-a-name\n---\n\nbody"
    with pytest.raises(MemoryPathError, match="description"):
        store.write("n.md", content)


def test_write_rejects_oversize_description(store: MemoryStore) -> None:
    huge = "d" * (DESCRIPTION_MAX + 1)
    with pytest.raises(MemoryPathError, match="description exceeds"):
        store.write("n.md", mem("body", desc=huge))


def test_write_accepts_template_and_round_trips(store: MemoryStore) -> None:
    """given valid templated content, when written, then it is stored verbatim."""
    content = mem("Alice prefers email", name="alice", desc="Alice's contact prefs")
    store.write("notes/users/alice.md", content)
    assert (store.root / "notes" / "users" / "alice.md").read_text() == content


# ---------------------------------------------------------------------------
# list — surfaces the description (skills protocol), falls back for legacy files
# ---------------------------------------------------------------------------


def test_list_surfaces_description_for_templated_file(store: MemoryStore) -> None:
    store.write("notes/alice.md", mem("body", name="alice", desc="Alice's prefs"))
    [listed] = store.list()
    assert listed.description == "Alice's prefs", (
        "list must surface the frontmatter description"
    )


def test_list_returns_none_for_legacy_file(store: MemoryStore) -> None:
    """A frontmatter-less file lists with description=None, not an error."""
    (store.root / "legacy.md").write_text("just some old notes\n")
    [listed] = store.list()
    assert listed.relative_path == "legacy.md"
    assert listed.description is None, "legacy files have no description to surface"


# ---------------------------------------------------------------------------
# memory_list tool — rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_list_tool_renders_description_and_fallback(
    store: MemoryStore,
) -> None:
    store.write("notes/alice.md", mem("body", name="alice", desc="Alice's prefs"))
    (store.root / "legacy.md").write_text("old notes\n")
    tool = ListMemoriesTool(ToolContext(memory_store=store))

    result = await tool.run(ListMemoriesArgs())

    assert "notes/alice.md — Alice's prefs" in result.content, (
        "templated files render as 'path — description'"
    )
    assert "legacy.md\t" in result.content, "legacy files fall back to path + size"


# ---------------------------------------------------------------------------
# append — grows the body and refreshes the description
# ---------------------------------------------------------------------------


def test_append_creates_templated_file_for_new_path(store: MemoryStore) -> None:
    store.append("notes/journal.md", "first entry\n", "running journal")
    text = (store.root / "notes" / "journal.md").read_text()
    assert text.startswith("---\n"), "a brand-new append must produce a templated file"
    assert "first entry" in text
    [listed] = store.list()
    assert listed.description == "running journal"
    assert listed.relative_path == "notes/journal.md"


def test_append_refreshes_description_and_keeps_body(store: MemoryStore) -> None:
    store.write("journal.md", mem("entry 1\n", name="diary", desc="old summary"))
    store.read("journal.md")  # unlock the read-before-write gate
    store.append("journal.md", "entry 2\n", "new summary")

    text = (store.root / "journal.md").read_text()
    assert "entry 1" in text and "entry 2" in text, "old + new body must both survive"
    [listed] = store.list()
    assert listed.description == "new summary", "the latest description must win"


def test_append_to_legacy_file_adds_frontmatter_with_derived_name(
    store: MemoryStore,
) -> None:
    """A legacy file's first append migrates it onto the template; name = stem."""
    (store.root / "scratch.md").write_text("old line\n")
    store.read("scratch.md")
    store.append("scratch.md", "new line\n", "scratch notes")

    text = (store.root / "scratch.md").read_text()
    assert text.startswith("---\n"), "legacy file must gain frontmatter"
    assert "name: scratch" in text, "name is derived from the filename stem"
    assert "old line" in text and "new line" in text, "no content is lost on migration"


def test_append_description_survives_yaml_special_chars(store: MemoryStore) -> None:
    """A description with a colon must round-trip as valid YAML, not break it."""
    store.append("n.md", "body\n", "ratio is 3:1, see notes")
    [listed] = store.list()
    assert listed.description == "ratio is 3:1, see notes"
