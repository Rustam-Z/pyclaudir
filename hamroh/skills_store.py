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

from .storage.frontmatter import parse_frontmatter, require_name_and_description
from .storage.path_safety import resolve_under_root


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


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md's YAML frontmatter from its body.

    Thin wrapper over :func:`hamroh.storage.frontmatter.parse_frontmatter`
    that pins the skill-specific error type and wording.
    """
    return parse_frontmatter(text, error_cls=SkillsError, label="SKILL.md")


def _validate_skill_metadata(metadata: dict, expected_name: str) -> None:
    """Enforce the required spec constraints on frontmatter."""
    require_name_and_description(metadata, error_cls=SkillsError, label="SKILL.md")
    name = metadata["name"]
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
        """Resolve ``skills/<name>/SKILL.md``, traversal-hardened.

        Name validation is local (single-component names only); the
        symlink and containment hardening is shared with the storage
        stores via :func:`resolve_under_root`.
        """
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
        return resolve_under_root(self._root, f"{name}/SKILL.md", SkillsError, "skill")

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
            skill = self._load_skill_entry(entry)
            if skill is not None:
                out.append(skill)
        return out

    def _load_skill_entry(self, entry: Path) -> SkillFile | None:
        """Build a :class:`SkillFile` for ``entry`` or ``None`` to skip it.

        Returns ``None`` for non-skill directories (hidden, disabled,
        missing/symlinked ``SKILL.md``) and for malformed or unreadable
        skills — one bad skill shouldn't blind the agent to the valid
        ones, so we never raise here.
        """
        if not entry.is_dir() or entry.is_symlink():
            return None
        if entry.name.startswith(".") or entry.name in self._disabled:
            return None
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file() or skill_md.is_symlink():
            return None
        try:
            size = skill_md.stat().st_size
            text = skill_md.read_text(encoding="utf-8")
            metadata, _ = _parse_frontmatter(text)
            _validate_skill_metadata(metadata, entry.name)
        except (OSError, SkillsError):
            return None
        return SkillFile(
            name=entry.name,
            size_bytes=size,
            description=metadata["description"].strip(),
        )

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


def render_skills_index(store: SkillsStore) -> str:
    """Render the available-skills block preloaded into the system prompt.

    Lists each skill's name + description (Agent Skills "level 1" metadata)
    so the agent always knows what playbooks exist without first calling
    ``skill_list`` — mirroring how Claude Code preloads skill descriptions.
    Returns an empty string when there are no skills, so the caller can
    append it unconditionally without leaving a dangling header.
    """
    skills = store.list()
    if not skills:
        return ""
    lines = "\n".join(f"- **{s.name}** — {s.description}" for s in skills)
    return (
        "# Available skills\n\n"
        "These operator-curated playbooks live under skills/<name>/SKILL.md. "
        "Their names and descriptions are listed here so you always know what "
        'exists — load a full body with skill_read("<name>") when one is '
        "relevant. skill_list re-fetches this same list on demand.\n\n"
        f"{lines}\n"
    )
