"""Disk-backed stores for everything under ``data/``.

Each module is a thin wrapper around one subdirectory of ``data/``
(``data/memories/``, ``data/attachments/``, ``data/renders/``) that
does path-safety hardening, size capping, and read/write helpers.
The shape is the same everywhere: a ``Store`` class with
``ensure_root``, ``resolve_path``, plus per-kind read/write methods.

Operator-curated content lives elsewhere — system prompt is at
``pyclaudir.instructions_store``, skill playbooks are at
``pyclaudir.skills_store`` — because those manage ``prompts/`` and
``skills/``, not ``data/``.

This package re-exports the public API so callers can keep writing
``from pyclaudir.storage import MemoryStore`` etc. without caring
which submodule actually houses each class.
"""

from __future__ import annotations

from .attachments import (
    MAX_TEXT_BYTES,
    AttachmentPathError,
    AttachmentStore,
    ImageAttachment,
    TextAttachment,
)
from .memory import (
    MAX_MEMORY_BYTES,
    MemoryFile,
    MemoryPathError,
    MemoryStore,
)
from .render import Render, RenderPathError, RenderStore

__all__ = [
    "MAX_MEMORY_BYTES",
    "MAX_TEXT_BYTES",
    "AttachmentPathError",
    "AttachmentStore",
    "ImageAttachment",
    "MemoryFile",
    "MemoryPathError",
    "MemoryStore",
    "Render",
    "RenderPathError",
    "RenderStore",
    "TextAttachment",
]
