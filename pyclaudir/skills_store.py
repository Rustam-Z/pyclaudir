"""Read-only access to agent skill playbooks at ``skills/<name>/SKILL.md``.

**Spec compliance:** this store implements the Agent Skills
specification (https://agentskills.io/specification). Every SKILL.md
must have YAML frontmatter with at least ``name`` and ``description``
fields; the ``name`` must match the parent directory name and follow
the ``[a-z0-9-]`` naming rules.

Skills are operator-curated markdown files that describe multi-step
agent workflows. The bot loads them via the :mod:`.tools.skills` MCP
tools — either through explicit invocation (a reminder-envelope
`<skill name="...">run</skill>` directive) or agent-side discovery
using the ``description`` metadata.

This store is **read-only** — the bot never writes to `skills/`.
The layout is strict:

- Only first-level directories directly under ``skills/`` count as
  skills.
- A directory only counts as a skill if it contains a ``SKILL.md``
  file with valid frontmatter. Other files/dirs alongside (README,
  helper scripts, references/, assets/, scripts/) are allowed but
  invisible to the list-surface.
- Path resolution is hardened the same way :class:`MemoryStore` does
  it: no ``..``, no absolute names, no symlinks anywhere in the
  chain, and the resolved path must stay inside ``skills/``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


class SkillsError(ValueError):
    """Raised when a skill name is rejected or a file is missing."""


@dataclass(frozen=True)
class SkillFile:
    name: str
    size_bytes: int
    description: str


#: Larger cap than memory files (playbooks can be substantial).
MAX_SKILL_BYTES = 256 * 1024

#: Agent Skills spec: name is 1-64 chars, lowercase letters/digits/hyphens,
#: no leading/trailing hyphen, no consecutive hyphens.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Frontmatter description cap per spec.
_DESCRIPTION_MAX = 1024
_NAME_MAX = 64


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from the markdown body.

    Returns ``(metadata, body)``. Raises :class:`SkillsError` if the
    frontmatter block is malformed.
    """
    if not text.startswith("---"):
        raise SkillsError("SKILL.md must start with YAML frontmatter delimiter '---'")
    # Match the frontmatter block: --- ... --- (line-anchored)
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if m is None:
        raise SkillsError("SKILL.md frontmatter block is not closed with '---'")
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillsError(f"SKILL.md frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillsError("SKILL.md frontmatter must be a YAML mapping")
    return data, text[m.end():]


def _validate_skill_metadata(metadata: dict, expected_name: str) -> None:
    """Enforce the required spec constraints on frontmatter."""
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise SkillsError("SKILL.md frontmatter must include a non-empty 'name' field")
    if len(name) > _NAME_MAX:
        raise SkillsError(f"skill name '{name}' exceeds {_NAME_MAX} chars")
    if not _NAME_RE.match(name):
        raise SkillsError(
            f"skill name '{name}' must be lowercase alphanumeric with hyphens, "
            "no leading/trailing/consecutive hyphens"
        )
    if name != expected_name:
        raise SkillsError(
            f"skill name '{name}' in frontmatter must match parent directory "
            f"'{expected_name}'"
        )
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillsError(
            "SKILL.md frontmatter must include a non-empty 'description' field"
        )
    if len(description) > _DESCRIPTION_MAX:
        raise SkillsError(
            f"skill description exceeds {_DESCRIPTION_MAX} chars ({len(description)})"
        )


class SkillsStore:
    def __init__(
        self,
        root: Path,
        *,
        disabled: frozenset[str] = frozenset(),
    ) -> None:
        #: ``resolve(strict=False)`` — root may not exist on a fresh clone
        #: of someone who hasn't seeded any skills. :meth:`ensure_root`
        #: creates the top-level dir.
        self._root = root.resolve()
        #: Skill directory names hidden from :meth:`list` and
        #: :meth:`read`. Sourced from ``plugins.json`` ``skills_disabled``.
        #: Filtered out before the SKILL.md is even read, so a malformed
        #: disabled skill never blocks the rest of the catalogue.
        self._disabled = disabled

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path safety — mirrors MemoryStore.resolve_path, adapted for names
    # (single-component skill identifiers only)
    # ------------------------------------------------------------------

    def _resolve_skill_md(self, name: str) -> Path:
        if name is None or name == "":
            raise SkillsError("skill name must be a non-empty string")
        if os.path.isabs(name):
            raise SkillsError(f"skill name must be relative, got {name!r}")
        parts = Path(name).parts
        if any(p == ".." for p in parts):
            raise SkillsError(f"skill name may not contain '..': {name!r}")
        # Enforce single-component name — no nested skills in v1.
        if len(parts) != 1:
            raise SkillsError(
                f"skill name must be a single directory name, got {name!r}"
            )

        skill_dir = self._root / parts[0]
        # Reject symlinks at the skill dir or its SKILL.md.
        try:
            if skill_dir.is_symlink():
                raise SkillsError(f"symlink in skills path: {skill_dir}")
        except OSError as exc:
            raise SkillsError(f"could not stat {skill_dir}: {exc}") from exc

        skill_md = skill_dir / "SKILL.md"
        try:
            if skill_md.is_symlink():
                raise SkillsError(f"symlink in skills path: {skill_md}")
        except OSError as exc:
            raise SkillsError(f"could not stat {skill_md}: {exc}") from exc

        # Final containment check.
        try:
            resolved = skill_md.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SkillsError(f"could not resolve {skill_md}: {exc}") from exc
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise SkillsError(
                f"resolved skill path escapes root: {resolved} not under {self._root}"
            ) from exc

        return resolved

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def list(self) -> list[SkillFile]:
        """Return every valid skill directly under ``skills/``.

        A directory counts as a valid skill only if it contains a
        ``SKILL.md`` with valid frontmatter (``name`` matching the
        directory, non-empty ``description``). Invalid frontmatter is
        silently skipped — a broken skill shouldn't take down the
        list surface — but is logged via Python's logging when
        debugging is enabled.

        Implements Agent Skills progressive disclosure: only the
        metadata (name + description from the frontmatter) is loaded
        here. Full bodies come back only via :meth:`read`.
        """
        if not self._root.exists():
            return []
        out: list[SkillFile] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name.startswith("."):
                continue
            if entry.name in self._disabled:
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file() or skill_md.is_symlink():
                continue
            try:
                size = skill_md.stat().st_size
                text = skill_md.read_text(encoding="utf-8")
                metadata, _ = _parse_frontmatter(text)
                _validate_skill_metadata(metadata, entry.name)
            except (OSError, SkillsError):
                # Malformed or unreadable skill — skip. We don't raise
                # because one bad skill shouldn't blind the agent to
                # the valid ones.
                continue
            out.append(
                SkillFile(
                    name=entry.name,
                    size_bytes=size,
                    description=metadata["description"].strip(),
                )
            )
        return out

    def read(self, name: str, max_bytes: int = MAX_SKILL_BYTES) -> str:
        """Read ``skills/<name>/SKILL.md`` as UTF-8, truncating if larger
        than ``max_bytes``. Frontmatter is kept in the returned text
        so consumers see the same bytes the spec describes.

        Also validates the frontmatter before returning — an invalid
        skill raises :class:`SkillsError` rather than surfacing
        partial content.
        """
        if name in self._disabled:
            raise SkillsError(f"skill not found: {name}")
        path = self._resolve_skill_md(name)
        if not path.exists() or not path.is_file():
            raise SkillsError(f"skill not found: {name}")
        raw = path.read_bytes()
        truncated = False
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
            truncated = True
        text = raw.decode("utf-8", errors="replace")
        # Validate frontmatter; raise if malformed (don't return a
        # broken skill).
        metadata, _ = _parse_frontmatter(text)
        _validate_skill_metadata(metadata, name)
        if truncated:
            text += f"\n\n[truncated to {max_bytes} bytes]"
        return text
