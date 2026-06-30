"""Committed read-only overlay: reads span both roots, writes stay runtime.

The store can overlay a git-tracked committed root on top of the writable
runtime root. These tests pin the contract: reads/listings/searches see both
roots, the runtime copy shadows a committed file at the same path, and writes
never touch the committed root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hamroh.storage.memory import MemoryPathError, MemoryStore

_TEMPLATE = "---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(runtime_root, committed_root)``, both created and empty."""
    runtime = tmp_path / "runtime"
    committed = tmp_path / "committed"
    runtime.mkdir()
    committed.mkdir()
    return runtime, committed


@pytest.fixture()
def store(roots: tuple[Path, Path]) -> MemoryStore:
    runtime, committed = roots
    s = MemoryStore(runtime, committed_root=committed)
    s.ensure_root()
    return s


def _seed(root: Path, relative: str, *, name: str, desc: str, body: str) -> None:
    """Write a templated memory file under ``root``."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE.format(name=name, desc=desc, body=body))


# ---------------------------------------------------------------------------
# list / read / search span both roots
# ---------------------------------------------------------------------------


def test_list_merges_runtime_and_committed(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """Given a file in each root, list returns both with their descriptions."""
    runtime, committed = roots
    _seed(runtime, "live.md", name="live", desc="runtime note", body="a")
    _seed(committed, "notes/ref.md", name="ref", desc="committed note", body="b")

    listed = {f.relative_path: f.description for f in store.list()}

    assert listed == {
        "live.md": "runtime note",
        "notes/ref.md": "committed note",
    }, "list must span both the runtime and committed roots"


def test_runtime_shadows_committed_on_same_path(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """When both roots hold the same path, the live runtime copy wins."""
    runtime, committed = roots
    _seed(committed, "dup.md", name="dup", desc="committed", body="OLD")
    _seed(runtime, "dup.md", name="dup", desc="runtime", body="NEW")

    listing = [f for f in store.list() if f.relative_path == "dup.md"]
    body = store.read("dup.md")

    assert len(listing) == 1, "a shadowed path must appear exactly once"
    assert listing[0].description == "runtime", "the runtime description wins"
    assert "NEW" in body and "OLD" not in body, "read returns the runtime copy"


def test_read_finds_committed_only_file(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """A file present only in the committed root is readable."""
    _, committed = roots
    _seed(committed, "notes/ref.md", name="ref", desc="committed", body="hello")

    assert "hello" in store.read("notes/ref.md"), "committed-only files must read"


def test_search_spans_committed(store: MemoryStore, roots: tuple[Path, Path]) -> None:
    """Keyword search finds matches inside committed files too."""
    _, committed = roots
    _seed(committed, "ref.md", name="ref", desc="d", body="the budget was approved")

    hits = store.search("budget")

    assert [h.relative_path for h in hits] == ["ref.md"], (
        "search must look inside committed files, not just runtime ones"
    )


# ---------------------------------------------------------------------------
# writes never touch the committed root
# ---------------------------------------------------------------------------


def test_write_targets_runtime_not_committed(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """A write lands under the runtime root, leaving the committed root empty."""
    runtime, committed = roots

    store.write("new.md", _TEMPLATE.format(name="n", desc="d", body="x"))

    assert (runtime / "new.md").is_file(), "write must create the file in runtime"
    assert not (committed / "new.md").exists(), "write must never touch committed"


def test_reading_committed_does_not_unlock_runtime_overwrite(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """Reading a committed file must not credit a read of a runtime twin.

    Otherwise reading the committed `dup.md` would let the bot overwrite the
    runtime `dup.md` it never actually saw — bypassing read-before-write.
    """
    runtime, committed = roots
    _seed(committed, "dup.md", name="dup", desc="committed", body="committed body")
    _seed(runtime, "dup.md", name="dup", desc="runtime", body="runtime body")

    store.read("dup.md")  # resolves to the runtime copy (it shadows committed)

    # The runtime copy was the one read, so this overwrite is allowed and the
    # committed copy is left untouched.
    store.write("dup.md", _TEMPLATE.format(name="dup", desc="d", body="z"))
    assert "z" in (runtime / "dup.md").read_text(), "runtime copy is overwritten"
    assert "committed body" in (committed / "dup.md").read_text(), (
        "the committed copy must never be touched by a write"
    )


def test_committed_read_alone_does_not_unlock_write(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """Reading a committed-only file leaves the write gate closed for that path.

    A later runtime file at the same path must still demand its own read.
    """
    runtime, committed = roots
    _seed(committed, "ref.md", name="ref", desc="committed", body="seen in committed")
    store.read("ref.md")  # only the committed copy exists, so this is what we read

    # Now a runtime file appears at the same path (e.g. operator copied it in).
    _seed(runtime, "ref.md", name="ref", desc="runtime", body="never read")

    with pytest.raises(MemoryPathError, match="read-before-write"):
        store.write("ref.md", _TEMPLATE.format(name="ref", desc="d", body="z"))


# ---------------------------------------------------------------------------
# resolve_readable
# ---------------------------------------------------------------------------


def test_resolve_readable_raises_for_missing(store: MemoryStore) -> None:
    """A path absent from both roots raises a clear not-found error."""
    with pytest.raises(MemoryPathError, match="not found"):
        store.resolve_readable("nope.md")


def test_no_committed_root_behaves_like_single_root(tmp_path: Path) -> None:
    """Without a committed root, the store still works over the runtime root."""
    s = MemoryStore(tmp_path / "mem")
    s.ensure_root()
    _seed(s.root, "a.md", name="a", desc="only", body="x")

    assert [f.relative_path for f in s.list()] == ["a.md"], (
        "a store with no committed root lists only its runtime files"
    )
