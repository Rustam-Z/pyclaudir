"""File-backed store for HTML→PNG renders under ``data/renders/``.

Same path-traversal hardening as :class:`pyclaudir.storage.memory.MemoryStore`
and :class:`pyclaudir.storage.attachments.AttachmentStore`: no ``..``, no
absolute paths, no symlinks, must stay inside the renders root.

Writers: ``render_html`` tool only. Reader: ``send_photo`` tool only.
Operator handles cleanup; this module never deletes.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class RenderPathError(ValueError):
    """Raised when a render path is rejected by safety checks."""


@dataclass(frozen=True)
class Render:
    relative_path: str
    absolute_path: Path
    size_bytes: int


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(s: str | None, *, max_len: int = 40) -> str:
    if not s:
        return ""
    cleaned = _SLUG_RE.sub("-", s.lower()).strip("-")
    return cleaned[:max_len]


class RenderStore:
    """Writable store for rendered PNG snapshots."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, relative: str) -> Path:
        if relative is None or relative == "":
            raise RenderPathError("render path must be a non-empty string")
        if os.path.isabs(relative):
            raise RenderPathError(
                f"render path must be relative to {self._root}, got {relative!r}"
            )
        parts = Path(relative).parts
        if any(p == ".." for p in parts):
            raise RenderPathError(f"render path may not contain '..': {relative!r}")
        check = self._root
        for part in parts:
            check = check / part
            try:
                if check.is_symlink():
                    raise RenderPathError(f"symlink in render path: {check}")
            except OSError as exc:
                raise RenderPathError(f"could not stat {check}: {exc}") from exc
        candidate = self._root.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise RenderPathError(f"could not resolve {candidate}: {exc}") from exc
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise RenderPathError(
                f"resolved render path escapes root: {resolved} not under {self._root}"
            ) from exc
        return resolved

    def allocate(self, title: str | None = None) -> Path:
        """Reserve an absolute path for a new render. Caller writes the bytes.

        Filename is ``<utc-stamp>-<slug-or-empty>-<rand>.png`` so renders
        are easy to identify in the directory listing and collisions are
        statistically impossible. Returns the absolute path to write to.
        """
        self.ensure_root()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        rand = secrets.token_hex(3)
        slug = _slug(title)
        name = f"{stamp}-{slug}-{rand}.png" if slug else f"{stamp}-{rand}.png"
        return self._root / name

    def relative(self, absolute: Path) -> str:
        """Convert an absolute render path back to its store-relative form."""
        return str(absolute.resolve().relative_to(self._root))

    def stat(self, relative: str) -> Render:
        """Resolve + stat a render file. Used by the photo-sender."""
        path = self.resolve_path(relative)
        if not path.exists() or not path.is_file():
            raise RenderPathError(f"render not found: {relative}")
        return Render(
            relative_path=relative,
            absolute_path=path,
            size_bytes=path.stat().st_size,
        )
