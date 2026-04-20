"""File-backed store for the two prompt files (`system.md`, `project.md`).

Unlike :class:`MemoryStore` this is a strict two-file allowlist — names
are looked up in a dict, there is no path resolution, no traversal
surface, no way to reach any file other than the two specified.

Writes are guarded by the **read-before-write invariant** (same rule as
memory): before overwriting or appending to a file you must first call
:meth:`read` on it in this process. Creating fresh content in a file
that doesn't exist is not supported here — both files always exist in
a running deployment, and `project.md` is `.gitignore`'d so the operator
seeds it manually at setup time.

Every mutating operation copies the current file contents to
``<backup_dir>/<name>-<UTC timestamp>.md`` before writing, so a bad
edit is always a single ``mv`` away from being reverted.

The owner-DM gate that protects these tools is applied one layer up,
in :mod:`pyclaudir.tools.instructions` — this store itself assumes the
caller is authorized.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class InstructionsError(ValueError):
    """Raised for any rejection from the instructions store."""


@dataclass(frozen=True)
class InstructionFile:
    name: str
    exists: bool
    size_bytes: int


#: Logical name → on-disk filename, rooted at ``project_root``.
PROMPT_FILES: dict[str, str] = {
    "system": "prompts/system.md",
    "project": "prompts/project.md",
}

#: 10× headroom over current system.md (~12 KiB). Big enough to grow into,
#: small enough that a runaway write can't fill the disk.
MAX_INSTRUCTION_BYTES = 128 * 1024


class InstructionsStore:
    def __init__(self, project_root: Path, backup_dir: Path) -> None:
        self._root = project_root.resolve()
        self._backup_dir = backup_dir.resolve()
        #: Logical names the caller has read this process. Read-before-write
        #: rejects mutations to any file not in this set.
        self._read_names: set[str] = set()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def backup_dir(self) -> Path:
        return self._backup_dir

    def ensure_dirs(self) -> None:
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, name: str) -> Path:
        relative = PROMPT_FILES.get(name)
        if relative is None:
            raise InstructionsError(
                f"unknown instruction file: {name!r}. "
                f"Must be one of {sorted(PROMPT_FILES)}."
            )
        return self._root / relative

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def list(self) -> list[InstructionFile]:
        out: list[InstructionFile] = []
        for name in PROMPT_FILES:
            path = self._resolve(name)
            if path.exists() and path.is_file():
                out.append(InstructionFile(name=name, exists=True, size_bytes=path.stat().st_size))
            else:
                out.append(InstructionFile(name=name, exists=False, size_bytes=0))
        return out

    def read(self, name: str) -> str:
        path = self._resolve(name)
        if not path.exists() or not path.is_file():
            raise InstructionsError(f"instruction file not present on disk: {name}")
        text = path.read_text(encoding="utf-8")
        self._read_names.add(name)
        return text

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _backup(self, name: str) -> Path | None:
        """Copy current file contents to the backup dir. Returns the backup
        path, or None if the source file doesn't exist yet."""
        path = self._resolve(name)
        if not path.exists():
            return None
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = self._backup_dir / f"{name}-{self._timestamp()}.md"
        shutil.copy2(path, backup_path)
        return backup_path

    def _atomic_write_bytes(self, path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` via tmp+rename so a crash mid-write
        cannot leave the file half-written."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def write(self, name: str, content: str) -> tuple[int, Path | None]:
        """Overwrite the named instruction file.

        Returns ``(bytes_written, backup_path)``. Raises
        :class:`InstructionsError` if the file doesn't yet exist, if the
        size cap would be exceeded, or if the read-before-write rail is
        violated.
        """
        path = self._resolve(name)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_INSTRUCTION_BYTES:
            raise InstructionsError(
                f"content too large: {len(encoded)} bytes > {MAX_INSTRUCTION_BYTES} cap"
            )
        if not path.exists():
            raise InstructionsError(
                f"refusing to create {name}: the file must already exist on disk "
                "(prompts are seeded by the operator, not materialized by the bot)"
            )
        if name not in self._read_names:
            raise InstructionsError(
                f"refusing to overwrite {name}: must call read_instructions "
                "first in this session (read-before-write invariant)"
            )
        backup = self._backup(name)
        self._atomic_write_bytes(path, encoded)
        self._read_names.add(name)
        return len(encoded), backup

    def append(self, name: str, content: str) -> tuple[int, Path | None]:
        """Append to the named instruction file.

        Returns ``(new_total_bytes, backup_path)``. Same safety rails as
        :meth:`write`, plus a post-append size check.
        """
        path = self._resolve(name)
        if not path.exists():
            raise InstructionsError(f"refusing to append: {name} does not exist on disk")
        if name not in self._read_names:
            raise InstructionsError(
                f"refusing to append to {name}: must call read_instructions "
                "first in this session (read-before-write invariant)"
            )
        existing = path.read_bytes()
        encoded = content.encode("utf-8")
        new_size = len(existing) + len(encoded)
        if new_size > MAX_INSTRUCTION_BYTES:
            raise InstructionsError(
                f"append would exceed cap: {new_size} bytes > {MAX_INSTRUCTION_BYTES}"
            )
        backup = self._backup(name)
        self._atomic_write_bytes(path, existing + encoded)
        self._read_names.add(name)
        return new_size, backup
