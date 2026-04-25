"""InstructionsStore unit tests — read, append, size cap, atomic write,
backup-before-append."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.instructions_store import (
    MAX_INSTRUCTION_BYTES,
    InstructionsError,
    InstructionsStore,
)


def _make_store(tmp_path: Path) -> InstructionsStore:
    project_md = tmp_path / "prompts" / "project.md"
    project_md.parent.mkdir(parents=True)
    project_md.write_text("PROJECT v1\n")
    store = InstructionsStore(project_md_path=project_md, backup_dir=tmp_path / "backups")
    store.ensure_dirs()
    return store


def test_read_returns_file_text(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert "PROJECT v1" in store.read()


def test_read_raises_when_missing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.path.unlink()
    with pytest.raises(InstructionsError, match="not present"):
        store.read()


def test_append_grows_file_and_returns_new_size(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    new_size, backup = store.append("line 2\n")
    assert backup is not None
    assert backup.read_text() == "PROJECT v1\n"
    assert store.path.read_text() == "PROJECT v1\nline 2\n"
    assert new_size == len(b"PROJECT v1\nline 2\n")


def test_append_raises_when_missing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.path.unlink()
    with pytest.raises(InstructionsError, match="not present"):
        store.append("anything\n")


def test_append_rejects_post_size_over_cap(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    big = "y" * (MAX_INSTRUCTION_BYTES - 100)
    store.path.write_text(big)
    with pytest.raises(InstructionsError, match="would exceed cap"):
        store.append("z" * 200)


def test_append_creates_timestamped_backup(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _, backup = store.append("more\n")
    assert backup.parent == store.backup_dir
    assert backup.name.startswith("project-")
    assert backup.name.endswith(".md")


def test_append_is_atomic_leaves_no_tmp(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.append("more\n")
    leftovers = list(store.path.parent.glob("*.tmp"))
    assert leftovers == []
