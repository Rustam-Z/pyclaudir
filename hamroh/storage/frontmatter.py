"""Shared YAML frontmatter: parse, validate, render.

Both skill playbooks (``skills/<name>/SKILL.md``) and memory files
(``data/memories/...``) carry Agent-Skills-style frontmatter: a
``---``-delimited YAML block with at least ``name`` and ``description``.
This module is the single source of truth for splitting that block,
validating those two fields, and rendering them back out, so the two
stores can't drift apart.

Each function takes an ``error_cls`` and a ``label`` so the caller's own
exception type and file-kind wording show up in error messages
(``"SKILL.md ..."`` for skills, ``"memory file ..."`` for memory).
"""

from __future__ import annotations

import re

import yaml

#: Frontmatter field caps, shared by skills and memory (Agent Skills spec).
NAME_MAX = 64
DESCRIPTION_MAX = 1024

#: Matches the leading ``--- ... ---`` block, line-anchored.
_BLOCK_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def parse_frontmatter(
    text: str, *, error_cls: type[Exception], label: str
) -> tuple[dict, str]:
    """Split YAML frontmatter from the markdown body.

    Returns ``(metadata, body)``. Raises ``error_cls`` if the frontmatter
    block is missing or malformed.
    """
    if not text.startswith("---"):
        raise error_cls(f"{label} must start with YAML frontmatter delimiter '---'")
    match = _BLOCK_RE.match(text)
    if match is None:
        raise error_cls(f"{label} frontmatter block is not closed with '---'")
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise error_cls(f"{label} frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise error_cls(f"{label} frontmatter must be a YAML mapping")
    return data, text[match.end() :]


def require_name_and_description(
    metadata: dict, *, error_cls: type[Exception], label: str
) -> None:
    """Enforce a non-empty ``name`` (≤64) and ``description`` (≤1024)."""
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise error_cls(f"{label} frontmatter must include a non-empty 'name' field")
    if len(name) > NAME_MAX:
        raise error_cls(f"{label} name exceeds {NAME_MAX} chars")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise error_cls(
            f"{label} frontmatter must include a non-empty 'description' field"
        )
    if len(description) > DESCRIPTION_MAX:
        raise error_cls(
            f"{label} description exceeds {DESCRIPTION_MAX} chars ({len(description)})"
        )


def render_frontmatter(metadata: dict) -> str:
    """Render ``metadata`` as a ``--- ... ---`` YAML block (trailing newline).

    Serialised via ``yaml.safe_dump`` so arbitrary field values (colons,
    quotes, unicode) stay valid YAML. Key order is preserved.
    """
    block = yaml.safe_dump(
        metadata, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return f"---\n{block}---\n"
