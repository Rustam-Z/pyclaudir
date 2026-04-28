"""``send_memory_document`` — send a memory file to a chat as a document.

The narrow, secure first cut of "send a file out": locked to
``data/memories/`` via :meth:`MemoryStore.resolve_path`, so the agent
can only ship its own memory back — never an arbitrary path on disk.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

from ..transcript import log_outbound
from .base import BaseTool, ToolResult, record_outbound

log = logging.getLogger(__name__)

#: Telegram caption hard limit.
_CAPTION_LIMIT = 1024


class SendMemoryDocumentArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id.")
    path: str = Field(
        description=(
            "Relative path under data/memories/ — same shape as "
            "read_memory. No '..', no absolute paths, no symlinks."
        ),
    )
    caption: str | None = Field(
        default=None,
        max_length=_CAPTION_LIMIT,
        description="Optional plain-text caption shown under the file.",
    )
    reply_to_message_id: int | None = None


class SendMemoryDocumentTool(BaseTool):
    name = "send_memory_document"
    description = (
        "Send a memory file (under data/memories/) to a Telegram chat as a "
        "downloadable document. Path is locked to the memories root with the "
        "same hardening as read_memory. Use this when the user asks for a "
        "memory file as an attachment rather than pasted text — handy for "
        "csv/log/large markdown they want to save."
    )
    args_model = SendMemoryDocumentArgs

    async def run(self, args: SendMemoryDocumentArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        store = self.ctx.memory_store
        if store is None:
            return ToolResult(content="memory store unavailable", is_error=True)

        try:
            resolved = await asyncio.to_thread(store.resolve_path, args.path)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)

        if not resolved.exists() or not resolved.is_file():
            return ToolResult(
                content=f"memory file not found: {args.path}", is_error=True,
            )

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
            args.chat_id, message_id, args.path,
        )

        if self.ctx.on_chat_replied is not None:
            try:
                self.ctx.on_chat_replied(args.chat_id)
            except Exception:  # pragma: no cover
                pass

        transcript_text = f"[document] {args.path}"
        if args.caption:
            transcript_text += f" — {args.caption}"
        log_outbound(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=message_id,
            reply_to_id=args.reply_to_message_id,
            text=transcript_text,
        )

        await record_outbound(
            self.ctx,
            chat_id=args.chat_id,
            message_id=message_id,
            text=transcript_text,
            reply_to_id=args.reply_to_message_id,
        )

        return ToolResult(
            content=f"sent document message_id={message_id} ({resolved.name})",
            data={
                "message_id": message_id,
                "chat_id": args.chat_id,
                "filename": resolved.name,
                "path": args.path,
            },
        )
