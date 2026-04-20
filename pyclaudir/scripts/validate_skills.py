"""Validate every skill under ``skills/`` against the Agent Skills spec.

Usage:

    uv run python -m pyclaudir.scripts.validate_skills
    uv run python -m pyclaudir.scripts.validate_skills --skills path/to/skills

Exit codes:
- 0 — every skill valid
- 1 — at least one skill rejected
- 2 — CLI / I/O error

The underlying check reuses :class:`pyclaudir.skills_store.SkillsStore`'s
own frontmatter parser + validation, so this CLI cannot drift from the
runtime rules. Runs as a pre-commit / CI step to prevent shipping a
malformed skill.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pyclaudir.skills_store import (
    SkillsError,
    _parse_frontmatter,
    _validate_skill_metadata,
)


def _validate_one(skill_dir: Path) -> tuple[bool, str]:
    """Return ``(ok, message)`` for a single skill directory."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False, f"{skill_dir.name}: missing SKILL.md"
    if not skill_md.is_file() or skill_md.is_symlink():
        return False, f"{skill_dir.name}: SKILL.md is not a regular file"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"{skill_dir.name}: read error: {exc}"
    try:
        metadata, _body = _parse_frontmatter(text)
    except SkillsError as exc:
        return False, f"{skill_dir.name}: {exc}"
    try:
        _validate_skill_metadata(metadata, skill_dir.name)
    except SkillsError as exc:
        return False, f"{skill_dir.name}: {exc}"
    desc = metadata.get("description", "")
    return True, f"{skill_dir.name}: OK ({len(desc)} char description)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--skills",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "skills",
        help="Root of the skills/ directory to validate (default: project root's skills/)",
    )
    args = parser.parse_args(argv)

    root: Path = args.skills
    if not root.exists():
        print(f"skills root does not exist: {root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"skills root is not a directory: {root}", file=sys.stderr)
        return 2

    entries = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not entries:
        print("(no skills found — nothing to validate)")
        return 0

    failed = 0
    for entry in entries:
        ok, msg = _validate_one(entry)
        prefix = "✓" if ok else "✗"
        print(f"{prefix} {msg}")
        if not ok:
            failed += 1

    if failed:
        print(f"\n{failed} skill(s) failed validation", file=sys.stderr)
        return 1
    print(f"\nAll {len(entries)} skill(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
