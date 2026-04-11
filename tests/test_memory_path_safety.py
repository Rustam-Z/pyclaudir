"""Hostile-input tests for MemoryStore path resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pyclaudir.memory_store import MemoryPathError, MemoryStore


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    root = tmp_path / "memories"
    root.mkdir()
    s = MemoryStore(root)
    s.ensure_root()
    return s


def test_rejects_dotdot_traversal(store: MemoryStore) -> None:
    with pytest.raises(MemoryPathError):
        store.resolve_path("../../../etc/passwd")


def test_rejects_absolute_path(store: MemoryStore) -> None:
    with pytest.raises(MemoryPathError):
        store.resolve_path("/etc/passwd")


def test_rejects_dotdot_inside_subdir(store: MemoryStore) -> None:
    with pytest.raises(MemoryPathError):
        store.resolve_path("notes/../../etc/passwd")


def test_rejects_nested_traversal_out_of_root(store: MemoryStore) -> None:
    with pytest.raises(MemoryPathError):
        store.resolve_path("data/memories/../../../etc/passwd")


def test_rejects_symlink_at_root(store: MemoryStore, tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret.txt").write_text("nope")
    link = store.root / "escape"
    os.symlink(target, link)
    with pytest.raises(MemoryPathError):
        store.resolve_path("escape/secret.txt")


def test_rejects_nested_symlink_chain(store: MemoryStore, tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "file.md").write_text("content")
    intermediate = tmp_path / "intermediate"
    os.symlink(real_dir, intermediate)
    link = store.root / "evil"
    os.symlink(intermediate, link)
    with pytest.raises(MemoryPathError):
        store.resolve_path("evil/file.md")


def test_rejects_symlinked_file_inside_root(store: MemoryStore, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    link = store.root / "shortcut.md"
    os.symlink(outside, link)
    with pytest.raises(MemoryPathError):
        store.resolve_path("shortcut.md")


def test_rejects_empty_path(store: MemoryStore) -> None:
    with pytest.raises(MemoryPathError):
        store.resolve_path("")


def test_accepts_legitimate_path(store: MemoryStore) -> None:
    target = store.root / "user_preferences.md"
    target.write_text("Rustam prefers Russian")
    resolved = store.resolve_path("user_preferences.md")
    assert resolved == target.resolve()


def test_accepts_nested_legitimate_path(store: MemoryStore) -> None:
    sub = store.root / "people" / "alice"
    sub.mkdir(parents=True)
    (sub / "notes.md").write_text("loves cats")
    resolved = store.resolve_path("people/alice/notes.md")
    assert resolved.exists()


def test_read_round_trip(store: MemoryStore) -> None:
    (store.root / "fact.md").write_text("the sky is blue")
    assert "blue" in store.read("fact.md")


def test_read_truncates_large_files(store: MemoryStore) -> None:
    big = "x" * 200_000
    (store.root / "big.md").write_text(big)
    out = store.read("big.md", max_bytes=1024)
    assert "[truncated to 1024 bytes]" in out


def test_list_skips_dotfiles_and_symlinks(store: MemoryStore, tmp_path: Path) -> None:
    (store.root / "real.md").write_text("a")
    (store.root / ".hidden").write_text("b")
    outside = tmp_path / "out.txt"
    outside.write_text("c")
    os.symlink(outside, store.root / "link.md")
    listed = {f.relative_path for f in store.list()}
    assert "real.md" in listed
    assert ".hidden" not in listed
    assert "link.md" not in listed


def test_no_write_methods_exist() -> None:
    """v1 invariant: MemoryStore exposes no write/edit/delete API."""
    forbidden = {"write", "create", "delete", "edit", "remove", "save", "put", "unlink"}
    public = {n for n in dir(MemoryStore) if not n.startswith("_")}
    overlap = public & forbidden
    assert not overlap, f"unexpected write-shaped methods: {overlap}"
