"""Read-only file-backed memory under ``data/memories/``.

Path resolution is **path-traversal hardened** — any of the following must
raise :class:`MemoryPathError`:

- a component containing ``..``
- an absolute path
- a path whose canonical resolution leaves the memories root
- a path whose resolution traverses any symlink

These rules are tested in ``tests/test_memory_path_safety.py``. The class
intentionally exposes no write/edit/delete API in v1 — populating memory
files is an out-of-band operator job.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class MemoryPathError(ValueError):
    """Raised when a memory path is rejected by safety checks."""


@dataclass(frozen=True)
class MemoryFile:
    relative_path: str
    size_bytes: int


class MemoryStore:
    def __init__(self, root: Path) -> None:
        # ``resolve(strict=False)`` is fine: the root may not exist yet at
        # construction time. ``ensure_root`` creates it.
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def resolve_path(self, relative: str) -> Path:
        """Resolve ``relative`` inside the memories root, hardened.

        Rules (any failure raises :class:`MemoryPathError`):

        1. ``relative`` must not be empty.
        2. ``relative`` must not be absolute.
        3. ``relative`` must not contain a ``..`` component (literal — even
           if benign-looking, we reject it; safer than reasoning about it).
        4. The candidate full path's canonical form must remain inside the
           memories root.
        5. None of the components leading to the file may be a symlink.
        """
        if relative is None or relative == "":
            raise MemoryPathError("memory path must be a non-empty string")
        if os.path.isabs(relative):
            raise MemoryPathError(f"memory path must be relative, got {relative!r}")

        # Normalize separators *without* using os.path.normpath, which would
        # silently collapse ``..``. We split manually and check.
        parts = Path(relative).parts
        if any(p == ".." for p in parts):
            raise MemoryPathError(f"memory path may not contain '..': {relative!r}")

        candidate = self._root.joinpath(*parts)

        # Walk every parent up to the root and reject symlinks. We can't use
        # ``Path.resolve(strict=True)`` because that would silently follow
        # symlinks; we explicitly want to refuse them.
        check = self._root
        for part in parts:
            check = check / part
            try:
                if check.is_symlink():
                    raise MemoryPathError(f"symlink in memory path: {check}")
            except OSError as exc:
                raise MemoryPathError(f"could not stat {check}: {exc}") from exc

        # Final containment check via canonical resolution.
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise MemoryPathError(f"could not resolve {candidate}: {exc}") from exc

        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise MemoryPathError(
                f"resolved memory path escapes root: {resolved} not under {self._root}"
            ) from exc

        return resolved

    # ------------------------------------------------------------------
    # Read API (read-only in v1 — no write/delete/edit methods exist)
    # ------------------------------------------------------------------

    def list(self) -> list[MemoryFile]:
        """List every file under the memories root, recursively.

        Hidden files (``.gitkeep``, dotfiles) are skipped. Symlinked entries
        are skipped silently — they cannot be read by ``read`` either.
        """
        if not self._root.exists():
            return []
        out: list[MemoryFile] = []
        for path in sorted(self._root.rglob("*")):
            if path.is_dir() or path.is_symlink() or path.name.startswith("."):
                continue
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(self._root)
            except ValueError:  # pragma: no cover - rglob shouldn't escape
                continue
            out.append(MemoryFile(relative_path=str(rel), size_bytes=path.stat().st_size))
        return out

    def read(self, relative: str, max_bytes: int = 64 * 1024) -> str:
        """Read a memory file as UTF-8.

        Files larger than ``max_bytes`` are truncated and the truncation is
        marked in the returned string so the model knows what happened.
        """
        path = self.resolve_path(relative)
        if not path.exists() or not path.is_file():
            raise MemoryPathError(f"memory file not found: {relative}")
        raw = path.read_bytes()
        truncated = False
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
            truncated = True
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += f"\n\n[truncated to {max_bytes} bytes]"
        return text
