"""Shared traversal-hardened path resolution for the storage stores.

One implementation of the safety rules that :class:`MemoryStore`,
:class:`AttachmentStore` and :class:`RenderStore` all enforce, so a
security fix lands in one place instead of three.
"""

from __future__ import annotations

import os
from pathlib import Path


def _reject_symlinks(
    root: Path,
    parts: tuple[str, ...],
    error_cls: type[ValueError],
    noun: str,
) -> None:
    """Walk every component under ``root`` and reject symlinks.

    We can't use ``Path.resolve(strict=True)`` because that would silently
    follow symlinks; we explicitly want to refuse them.
    """
    check = root
    for part in parts:
        check = check / part
        try:
            if check.is_symlink():
                raise error_cls(f"symlink in {noun} path: {check}")
        except OSError as exc:
            raise error_cls(f"could not stat {check}: {exc}") from exc


def resolve_under_root(
    root: Path,
    relative: str,
    error_cls: type[ValueError],
    noun: str,
) -> Path:
    """Resolve ``relative`` inside ``root``, hardened.

    Rules (any failure raises ``error_cls``): non-empty, not absolute, no
    literal ``..`` component, no symlink anywhere on the path, and the
    canonical resolution must stay inside ``root``. ``noun`` names the
    store in error messages ("memory", "attachment", "render").
    """
    if relative is None or relative == "":
        raise error_cls(f"{noun} path must be a non-empty string")
    if os.path.isabs(relative):
        raise error_cls(f"{noun} path must be relative to {root}, got {relative!r}")

    # Split manually and check rather than normalising — os.path.normpath
    # would silently collapse ``..``.
    parts = Path(relative).parts
    if any(p == ".." for p in parts):
        raise error_cls(f"{noun} path may not contain '..': {relative!r}")

    _reject_symlinks(root, parts, error_cls, noun)

    # Final containment check via canonical resolution.
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise error_cls(f"could not resolve {candidate}: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise error_cls(
            f"resolved {noun} path escapes root: {resolved} not under {root}"
        ) from exc
    return resolved
