"""``telegram_send_memory_document`` — send a memory file to a chat as a document.

The narrow, secure first cut of "send a file out": locked to
``data/memories/`` via :meth:`MemoryStore.resolve_path`, so the agent
can only ship its own memory back — never an arbitrary path on disk.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ..base import BaseTool, OutboundDelivery, ToolResult, deliver_bookkeeping

if TYPE_CHECKING:
    from pathlib import Path

    from ...storage.memory import MemoryStore

log = logging.getLogger(__name__)

#: Telegram caption hard limit.
_CAPTION_LIMIT = 1024


class SendMemoryDocumentArgs(BaseModel):
    chat_id: int = Field(
        description=(
            "Numeric Telegram chat id (e.g. -1001234567890 for a group, a "
            "positive int for a DM). Not an @username."
        )
    )
    path: str = Field(
        description=(
            "Relative path under data/memories/ — same shape as "
            "memory_read. No '..', no absolute paths, no symlinks."
        ),
    )
    caption: str | None = Field(
        default=None,
        max_length=_CAPTION_LIMIT,
        description="Optional plain-text caption shown under the file (max 1024 chars).",
    )
    reply_to_message_id: int | None = Field(
        default=None,
        description=(
            "Optional. Quote-reply the document to this message id; omit for a "
            "standalone send."
        ),
    )


class TelegramSendMemoryDocumentTool(BaseTool[SendMemoryDocumentArgs]):
    name = "telegram_send_memory_document"
    description = (
        "Send a memory file (from data/memories/) to a chat as a downloadable "
        "document. Use when the user asks for a memory file as an attachment "
        "rather than pasted text — handy for csv/log/large markdown. For a "
        "rendered image use telegram_send_photo; for plain text use "
        "telegram_send_message. Path-locked to the memories root (same "
        "hardening as memory_read); sends immediately."
    )
    args_model = SendMemoryDocumentArgs

    async def run(self, args: SendMemoryDocumentArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="memory store unavailable", is_error=True)

        resolved = await _resolve_memory(store, args.path)
        if isinstance(resolved, ToolResult):
            return resolved

        sent = await self.ctx.bot.send_document(
            chat_id=args.chat_id,
            document=resolved,
            filename=resolved.name,
            caption=args.caption,
            reply_to_message_id=args.reply_to_message_id,
        )
        message_id = sent.message_id
        log.info(
            "hot-path stage=delivered chat=%s msg=%s document=%s",
            args.chat_id,
            message_id,
            args.path,
        )

        await deliver_bookkeeping(
            self.ctx,
            OutboundDelivery(
                chat_id=args.chat_id,
                message_id=message_id,
                reply_to_id=args.reply_to_message_id,
                transcript_text=_transcript_text(args.path, args.caption),
            ),
        )
        return _build_result(args, message_id, resolved.name)


def _build_result(
    args: SendMemoryDocumentArgs, message_id: int, filename: str
) -> ToolResult:
    """Assemble the success result for a delivered document."""
    return ToolResult(
        content=f"sent document message_id={message_id} ({filename})",
        data={
            "message_id": message_id,
            "chat_id": args.chat_id,
            "filename": filename,
            "path": args.path,
        },
    )


async def _resolve_memory(store: MemoryStore, path: str) -> Path | ToolResult:
    """Resolve ``path`` under the memories root.

    Returns the resolved path on success, or an error ``ToolResult`` when the
    path is rejected by the store's safety checks or points at a missing file.
    """
    try:
        resolved = await asyncio.to_thread(store.resolve_path, path)
    except Exception as exc:
        return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
    if not resolved.exists() or not resolved.is_file():
        return ToolResult(content=f"memory file not found: {path}", is_error=True)
    return resolved


def _transcript_text(path: str, caption: str | None) -> str:
    """Render the transcript line for a delivered document, with optional caption."""
    if caption:
        return f"[document] {path} — {caption}"
    return f"[document] {path}"
