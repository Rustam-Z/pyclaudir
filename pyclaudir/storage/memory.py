"""File-backed memory under ``data/memories/``.

Path resolution is **path-traversal hardened** — any of the following must
raise :class:`MemoryPathError`:

- a component containing ``..``
- an absolute path
- a path whose canonical resolution leaves the memories root
- a path whose resolution traverses any symlink

These rules apply to **both reads and writes** and are tested in
``tests/test_memory_path_safety.py``.

Writes are guarded by the **read-before-write invariant** (Claudir Part 3):
before overwriting or appending to an existing file you must first read it
in this process. This stops the model from blindly destroying operator-
curated notes whose content it never observed. Creating a new file (one
that doesn't yet exist) is always allowed because there's nothing to lose.
The "read paths" set lives in this instance and resets on process restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_safety import resolve_under_root


class MemoryPathError(ValueError):
    """Raised when a memory path is rejected by safety checks."""


@dataclass(frozen=True)
class MemoryFile:
    relative_path: str
    size_bytes: int


#: Maximum size of any one memory file. Matches the read-truncation default
#: so a file the model can read fully can also be re-written fully.
MAX_MEMORY_BYTES = 64 * 1024


class MemoryStore:
    def __init__(self, root: Path) -> None:
        # ``resolve(strict=False)`` is fine: the root may not exist yet at
        # construction time. ``ensure_root`` creates it.
        self._root = root.resolve()
        #: Set of relative paths read in this process. The read-before-write
        #: rule rejects mutating writes to any path not in this set. New
        #: files (which don't yet exist) are exempt — there's nothing to
        #: have read.
        self._read_paths: set[str] = set()

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def read_paths_snapshot(self) -> frozenset[str]:
        """Test/inspection helper — frozen snapshot of paths read this run."""
        return frozenset(self._read_paths)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def resolve_path(self, relative: str) -> Path:
        """Resolve ``relative`` inside the memories root, hardened.

        See :func:`pyclaudir.storage.path_safety.resolve_under_root` for
        the rules; any failure raises :class:`MemoryPathError`.
        """
        return resolve_under_root(self._root, relative, MemoryPathError, "memory")

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

    def read(self, relative: str, max_bytes: int = MAX_MEMORY_BYTES) -> str:
        """Read a memory file as UTF-8.

        Files larger than ``max_bytes`` are truncated and the truncation is
        marked in the returned string so the model knows what happened.
        Records the path in :attr:`_read_paths` so the read-before-write
        gate will allow subsequent writes to the same path.
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
        # Record the read AFTER we successfully decoded — so a path that
        # raised never gets credited.
        self._read_paths.add(relative)
        return text

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def write(self, relative: str, content: str) -> int:
        """Create or overwrite a memory file with ``content``.

        Returns the number of bytes written.

        Rules:

        - Path resolution must pass :meth:`resolve_path` (no traversal,
          no symlinks, must stay inside root).
        - ``content`` UTF-8 byte length must be ≤ :data:`MAX_MEMORY_BYTES`.
        - If the file already exists, ``relative`` must be in
          :attr:`_read_paths` (read-before-write). New files are exempt.
        """
        path = self.resolve_path(relative)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_MEMORY_BYTES:
            raise MemoryPathError(
                f"memory file too large: {len(encoded)} bytes > {MAX_MEMORY_BYTES} cap"
            )
        if path.exists():
            if relative not in self._read_paths:
                raise MemoryPathError(
                    f"refusing to overwrite {relative}: must call read_memory "
                    "first in this session (read-before-write invariant)"
                )
            if not path.is_file():
                raise MemoryPathError(f"{relative} exists but is not a regular file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        # We just wrote it — credit the read so subsequent overwrites in the
        # same session are allowed without an extra round-trip.
        self._read_paths.add(relative)
        return len(encoded)

    def append(self, relative: str, content: str) -> int:
        """Append ``content`` to a memory file.

        Returns the new total size in bytes.

        Same path safety + read-before-write rules as :meth:`write`. The
        post-append size must still fit within :data:`MAX_MEMORY_BYTES`.
        """
        path = self.resolve_path(relative)
        encoded = content.encode("utf-8")
        existing = b""
        if path.exists():
            if relative not in self._read_paths:
                raise MemoryPathError(
                    f"refusing to append to {relative}: must call read_memory "
                    "first in this session (read-before-write invariant)"
                )
            if not path.is_file():
                raise MemoryPathError(f"{relative} exists but is not a regular file")
            existing = path.read_bytes()
        new_size = len(existing) + len(encoded)
        if new_size > MAX_MEMORY_BYTES:
            raise MemoryPathError(
                f"append would exceed cap: {new_size} bytes > {MAX_MEMORY_BYTES}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as fh:
            fh.write(encoded)
        self._read_paths.add(relative)
        return new_size
