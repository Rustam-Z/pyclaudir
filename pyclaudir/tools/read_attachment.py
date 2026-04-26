"""``read_attachment`` — read a Telegram attachment by relative path.

Companion to the dispatcher's attachment ingest in ``telegram_io.py``. Path
resolution is locked to ``data/attachments/``: same traversal-hardened
rules as ``read_memory`` (no ``..``, no absolute paths, no symlinks). The
absolute paths the dispatcher writes into ``[attachment: ...]`` markers
land *under* the attachments root, so the model passes the relative tail
back into this tool — or it hands the absolute path and we strip the root.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult


class ReadAttachmentArgs(BaseModel):
    path: str = Field(
        description=(
            "Either the absolute path printed in the inbound "
            "``[attachment: ...]`` marker, or a path relative to "
            "``data/attachments/``. Path traversal (``..``) and symlinks are "
            "rejected."
        ),
    )


class ReadAttachmentTool(BaseTool):
    name = "read_attachment"
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

        # Accept absolute paths that fall under the store root by stripping
        # the root prefix. Anything outside the root is rejected by
        # ``resolve_path`` regardless.
        raw = args.path
        try:
            p = Path(raw)
            if p.is_absolute():
                relative = str(p.resolve().relative_to(store.root))
            else:
                relative = raw
        except ValueError:
            return ToolResult(
                content=f"path is not under attachments root: {raw}",
                is_error=True,
            )

        try:
            resolved = await asyncio.to_thread(store.resolve_path, relative)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)

        kind = store.kind(resolved)
        if kind == "image":
            try:
                img = await asyncio.to_thread(store.open_image, relative)
            except Exception as exc:
                return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
            return ToolResult(
                content=f"image attachment {relative} ({img.mime}, {img.size_bytes} bytes)",
                data={"path": str(img.path), "mime": img.mime, "size_bytes": img.size_bytes},
                image_path=img.path,
            )

        if kind == "text":
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

        if kind == "pdf":
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

        return ToolResult(
            content=f"attachment {relative} has unsupported kind for reading",
            is_error=True,
        )
