"""SkillsStore — read-only, path-hardened, Agent Skills spec-compliant.

Covers spec conformance (frontmatter required, name/description rules,
directory/name match) plus the pyclaudir-specific hardening (path
traversal, symlinks, size cap).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pyclaudir.skills_store import MAX_SKILL_BYTES, SkillsError, SkillsStore


_VALID_FRONTMATTER = textwrap.dedent(
    """\
    ---
    name: {name}
    description: A test skill used by the SkillsStore test suite.
    ---

    # {name}

    Body content.
    """
)


def _make_store(tmp_path: Path) -> SkillsStore:
    root = tmp_path / "skills"
    (root / "self-reflection").mkdir(parents=True)
    (root / "self-reflection" / "SKILL.md").write_text(
        _VALID_FRONTMATTER.format(name="self-reflection")
    )
    (root / "self-reflection" / "README.md").write_text("readme\n")
    (root / "another").mkdir()
    (root / "another" / "SKILL.md").write_text(
        _VALID_FRONTMATTER.format(name="another")
    )
    # A dir without SKILL.md should be ignored.
    (root / "docs-only").mkdir()
    (root / "docs-only" / "notes.md").write_text("notes\n")
    store = SkillsStore(root=root)
    store.ensure_root()
    return store


# ---------------------------------------------------------------------------
# list() — spec-compliant discovery
# ---------------------------------------------------------------------------


def test_list_returns_only_dirs_with_valid_skill_md(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    names = [f.name for f in store.list()]
    assert sorted(names) == ["another", "self-reflection"]
    assert "docs-only" not in names


def test_list_includes_description_from_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    files = {f.name: f for f in store.list()}
    assert "SkillsStore test suite" in files["self-reflection"].description


def test_list_skips_skill_with_invalid_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Break 'another' by stripping frontmatter entirely.
    (store.root / "another" / "SKILL.md").write_text("# No frontmatter here\n")
    names = [f.name for f in store.list()]
    assert names == ["self-reflection"]  # 'another' silently dropped


def test_list_skips_skill_with_name_mismatch(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Frontmatter name doesn't match dir name.
    (store.root / "another" / "SKILL.md").write_text(
        _VALID_FRONTMATTER.format(name="totally-different")
    )
    names = [f.name for f in store.list()]
    assert names == ["self-reflection"]


def test_list_on_missing_root_returns_empty(tmp_path: Path) -> None:
    store = SkillsStore(root=tmp_path / "does-not-exist")
    assert store.list() == []


# ---------------------------------------------------------------------------
# read() — returns body, validates frontmatter
# ---------------------------------------------------------------------------


def test_read_returns_skill_content_including_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    text = store.read("self-reflection")
    assert text.startswith("---")
    assert "name: self-reflection" in text
    assert "# self-reflection" in text


def test_read_rejects_invalid_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "self-reflection" / "SKILL.md").write_text("# No frontmatter\n")
    with pytest.raises(SkillsError, match="frontmatter"):
        store.read("self-reflection")


def test_read_unknown_skill_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="skill not found"):
        store.read("does-not-exist")


# ---------------------------------------------------------------------------
# Frontmatter validation — spec rules
# ---------------------------------------------------------------------------


def test_frontmatter_missing_name_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "self-reflection" / "SKILL.md").write_text(
        "---\ndescription: No name field\n---\n\nbody\n"
    )
    with pytest.raises(SkillsError, match="'name'"):
        store.read("self-reflection")


def test_frontmatter_missing_description_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "self-reflection" / "SKILL.md").write_text(
        "---\nname: self-reflection\n---\n\nbody\n"
    )
    with pytest.raises(SkillsError, match="'description'"):
        store.read("self-reflection")


def test_frontmatter_name_format_rejected(tmp_path: Path) -> None:
    # Uppercase is a spec violation.
    store = _make_store(tmp_path)
    (store.root / "self-reflection" / "SKILL.md").write_text(
        "---\nname: Self-Reflection\ndescription: bad name case.\n---\n"
    )
    with pytest.raises(SkillsError, match="name"):
        store.read("self-reflection")


def test_frontmatter_leading_hyphen_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "-badname").mkdir()
    (store.root / "-badname" / "SKILL.md").write_text(
        "---\nname: -badname\ndescription: leading hyphen is forbidden.\n---\n"
    )
    with pytest.raises(SkillsError, match="hyphen"):
        store.read("-badname")


def test_frontmatter_consecutive_hyphens_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "bad--name").mkdir()
    (store.root / "bad--name" / "SKILL.md").write_text(
        "---\nname: bad--name\ndescription: consecutive hyphens forbidden.\n---\n"
    )
    with pytest.raises(SkillsError, match="hyphen"):
        store.read("bad--name")


def test_frontmatter_description_too_long(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    huge = "x" * 1100  # spec cap is 1024
    (store.root / "self-reflection" / "SKILL.md").write_text(
        f"---\nname: self-reflection\ndescription: {huge}\n---\n"
    )
    with pytest.raises(SkillsError, match="description exceeds"):
        store.read("self-reflection")


# ---------------------------------------------------------------------------
# Path hardening (unchanged from pyclaudir's own rules)
# ---------------------------------------------------------------------------


def test_read_rejects_traversal(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="may not contain"):
        store.read("../something")


def test_read_rejects_nested_name(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="single directory"):
        store.read("self-reflection/SKILL.md")


def test_read_rejects_absolute_path(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="must be relative"):
        store.read("/etc/passwd")


def test_read_rejects_empty_name(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="non-empty"):
        store.read("")


def test_read_rejects_symlink(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    other = tmp_path / "outside"
    other.mkdir()
    (other / "SKILL.md").write_text(_VALID_FRONTMATTER.format(name="evil"))
    (store.root / "evil").symlink_to(other)
    with pytest.raises(SkillsError, match="symlink"):
        store.read("evil")


def test_read_truncates_at_cap(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Write a skill with valid frontmatter but a giant body.
    body = "y" * (MAX_SKILL_BYTES + 100)
    (store.root / "self-reflection" / "SKILL.md").write_text(
        f"---\nname: self-reflection\ndescription: big body.\n---\n{body}"
    )
    text = store.read("self-reflection")
    assert "[truncated" in text
