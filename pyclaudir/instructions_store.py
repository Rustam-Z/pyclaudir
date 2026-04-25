"""Read and append the project prompt at ``prompts/project.md``.

system.md is intentionally not exposed: it's git-tracked, so any bot
edit there would land as a working-tree diff and pollute the repo.
All operator-driven customisations accumulate in project.md
(gitignored), which is concatenated after system.md to form the full
prompt.

The owner-only policy is enforced in the system prompt, not here.
Code-level rails: file must already exist, 128 KiB cap, atomic write,
backup before mutate.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path


class InstructionsError(ValueError):
    """Raised for any rejection from the instructions store."""


#: 10× headroom over a typical project.md. Big enough to grow into,
#: small enough that a runaway append can't fill the disk.
MAX_INSTRUCTION_BYTES = 128 * 1024


class InstructionsStore:
    def __init__(self, project_md_path: Path, backup_dir: Path) -> None:
        self._path = project_md_path.resolve()
        self._backup_dir = backup_dir.resolve()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def backup_dir(self) -> Path:
        return self._backup_dir

    def ensure_dirs(self) -> None:
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    def read(self) -> str:
        if not self._path.exists():
            raise InstructionsError(f"project.md not present at {self._path}")
        return self._path.read_text(encoding="utf-8")

    def append(self, content: str) -> tuple[int, Path]:
        """Append ``content`` to project.md. Returns (new_total_bytes, backup_path)."""
        if not self._path.exists():
            raise InstructionsError(f"project.md not present at {self._path}")
        existing = self._path.read_bytes()
        encoded = content.encode("utf-8")
        new_size = len(existing) + len(encoded)
        if new_size > MAX_INSTRUCTION_BYTES:
            raise InstructionsError(
                f"append would exceed cap: {new_size} bytes > {MAX_INSTRUCTION_BYTES}"
            )
        backup = self._backup()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_bytes(existing + encoded)
        tmp.replace(self._path)
        return new_size, backup

    def _backup(self) -> Path:
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self._backup_dir / f"project-{ts}.md"
        shutil.copy2(self._path, backup_path)
        return backup_path
