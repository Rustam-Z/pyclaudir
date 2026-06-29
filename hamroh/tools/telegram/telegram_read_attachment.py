"""``telegram_read_attachment`` — read a Telegram attachment by relative path.

Companion to the dispatcher's attachment ingest in ``telegram_io.py``. Path
resolution is locked to ``data/attachments/``: same traversal-hardened
rules as ``memory_read`` (no ``..``, no absolute paths, no symlinks). The
absolute paths the dispatcher writes into ``[attachment: ...]`` markers
land *under* the attachments root, so the model passes the relative tail
back into this tool — or it hands the absolute path and we strip the root.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult

if TYPE_CHECKING:
    from ...storage.attachments import AttachmentStore


class ReadAttachmentArgs(BaseModel):
    path: str = Field(
        description=(
            "Either the absolute path printed in the inbound "
            "``[attachment: ...]`` marker, or a path relative to "
            "``data/attachments/``. Path traversal (``..``) and symlinks are "
            "rejected."
        ),
    )


class TelegramReadAttachmentTool(BaseTool[ReadAttachmentArgs]):
    name = "telegram_read_attachment"
    description = (
        "Read a Telegram attachment that the user sent. Inbound photos and "
        "documents are saved under data/attachments/ by the dispatcher and "
        "surfaced as ``[attachment: <path> ...]`` markers in the user's "
        "message. Pass that path here. Images are returned as image content "
        "blocks (so you can actually see them); text-like files (md, txt, "
        "log, csv, json, yaml, code, ...) are returned as UTF-8 text. PDFs "
        "are extracted via pypdf and returned as text with ``--- page N ---`` "
        "markers."
    )
    args_model = ReadAttachmentArgs

    async def run(self, args: ReadAttachmentArgs) -> ToolResult:
        store = self.ctx.attachment_store
        if store is None:
            return ToolResult(content="attachment store unavailable", is_error=True)

        relative = _to_relative(args.path, store.root)
        if relative is None:
            return ToolResult(
                content=f"path is not under attachments root: {args.path}",
                is_error=True,
            )

        try:
            resolved = await asyncio.to_thread(store.resolve_path, relative)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)

        kind = store.kind(resolved)
        if kind == "image":
            return await self._read_image(store, relative)
        if kind == "text":
            return await self._read_text(store, relative)
        if kind == "pdf":
            return await self._read_pdf(store, relative)
        return ToolResult(
            content=f"attachment {relative} has unsupported kind for reading",
            is_error=True,
        )

    async def _read_image(self, store: "AttachmentStore", relative: str) -> ToolResult:
        """Return an image attachment as an image content block."""
        try:
            img = await asyncio.to_thread(store.open_image, relative)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(
            content=f"image attachment {relative} ({img.mime}, {img.size_bytes} bytes)",
            data={
                "path": str(img.path),
                "mime": img.mime,
                "size_bytes": img.size_bytes,
            },
            image_path=img.path,
        )

    async def _read_text(self, store: "AttachmentStore", relative: str) -> ToolResult:
        """Return a text-like attachment as UTF-8 text."""
        try:
            txt = await asyncio.to_thread(store.read_text, relative)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(
            content=txt.text,
            data={
                "path": relative,
                "size_bytes": txt.size_bytes,
                "truncated": txt.truncated,
            },
        )

    async def _read_pdf(self, store: "AttachmentStore", relative: str) -> ToolResult:
        """Return a PDF attachment as extracted text with page markers."""
        try:
            txt = await asyncio.to_thread(store.read_pdf, relative)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(
            content=txt.text,
            data={
                "path": relative,
                "size_bytes": txt.size_bytes,
                "truncated": txt.truncated,
                "kind": "pdf",
            },
        )


def _to_relative(raw: str, root: Path) -> str | None:
    """Normalise an inbound path to one relative to the attachments root.

    Absolute paths under ``root`` are stripped to their tail; relative paths
    pass through unchanged. Returns ``None`` when an absolute path lies
    outside ``root`` (``resolve_path`` rejects anything that slips past).
    """
    p = Path(raw)
    if not p.is_absolute():
        return raw
    try:
        return str(p.resolve().relative_to(root))
    except ValueError:
        return None
