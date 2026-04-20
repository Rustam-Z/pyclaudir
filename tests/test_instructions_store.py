"""InstructionsStore unit tests — allowlist, size cap, read-before-write,
backup-before-write, atomic write."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.instructions_store import (
    MAX_INSTRUCTION_BYTES,
    InstructionsError,
    InstructionsStore,
)


def _make_store(tmp_path: Path) -> InstructionsStore:
    project_root = tmp_path / "proj"
    (project_root / "prompts").mkdir(parents=True)
    (project_root / "prompts" / "system.md").write_text("SYSTEM v1\n")
    (project_root / "prompts" / "project.md").write_text("PROJECT v1\n")
    backup_dir = tmp_path / "backups"
    store = InstructionsStore(project_root=project_root, backup_dir=backup_dir)
    store.ensure_dirs()
    return store


def test_list_returns_both_files(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    files = store.list()
    names = {f.name: f for f in files}
    assert set(names) == {"system", "project"}
    assert names["system"].exists
    assert names["system"].size_bytes > 0


def test_list_marks_missing_file(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "prompts" / "project.md").unlink()
    files = {f.name: f for f in store.list()}
    assert files["project"].exists is False
    assert files["project"].size_bytes == 0


def test_read_unknown_name_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(InstructionsError):
        store.read("something-else")


def test_read_records_the_read(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    text = store.read("system")
    assert "SYSTEM v1" in text
    # Write is now allowed because we've read.
    store.write("system", "SYSTEM v2\n")


def test_write_requires_read_first(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(InstructionsError, match="read-before-write"):
        store.write("system", "SYSTEM v2\n")


def test_append_requires_read_first(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(InstructionsError, match="read-before-write"):
        store.append("project", "more text\n")


def test_write_rejects_missing_file(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "prompts" / "project.md").unlink()
    # Even with a read allowance, missing file can't be written.
    store._read_names.add("project")
    with pytest.raises(InstructionsError, match="must already exist"):
        store.write("project", "body\n")


def test_write_rejects_oversize(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.read("system")  # unlock
    huge = "x" * (MAX_INSTRUCTION_BYTES + 1)
    with pytest.raises(InstructionsError, match="too large"):
        store.write("system", huge)


def test_append_rejects_post_size_over_cap(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Start with a file already at (cap - 100) bytes.
    big = "y" * (MAX_INSTRUCTION_BYTES - 100)
    (store.root / "prompts" / "system.md").write_text(big)
    store.read("system")
    with pytest.raises(InstructionsError, match="would exceed cap"):
        store.append("system", "z" * 200)


def test_write_creates_backup(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.read("system")
    written, backup = store.write("system", "SYSTEM v2\n")
    assert written == len(b"SYSTEM v2\n")
    assert backup is not None
    assert backup.parent == store.backup_dir
    assert backup.name.startswith("system-")
    assert backup.name.endswith(".md")
    assert backup.read_text() == "SYSTEM v1\n"
    # Live file has the new content.
    assert (store.root / "prompts" / "system.md").read_text() == "SYSTEM v2\n"


def test_append_creates_backup(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.read("project")
    new_size, backup = store.append("project", "line 2\n")
    assert backup is not None
    assert backup.read_text() == "PROJECT v1\n"
    assert (store.root / "prompts" / "project.md").read_text() == "PROJECT v1\nline 2\n"
    assert new_size == len(b"PROJECT v1\nline 2\n")


def test_write_is_atomic_leaves_no_tmp(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.read("system")
    store.write("system", "SYSTEM v2\n")
    leftovers = list((store.root / "prompts").glob("*.tmp"))
    assert leftovers == []


def test_read_then_write_then_read_reflects_new_content(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.read("project")
    store.write("project", "BRAND NEW\n")
    # subsequent reads see the new content (and still count as reads)
    assert store.read("project") == "BRAND NEW\n"
