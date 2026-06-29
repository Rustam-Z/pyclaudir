"""``telegram_send_photo`` — send a rendered image to a chat as a Telegram photo.

Companion to :mod:`hamroh.tools.render_html`. Path is locked to the
renders root with the same hardening pattern as ``telegram_send_memory_document``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ..base import BaseTool, OutboundDelivery, ToolResult, deliver_bookkeeping

if TYPE_CHECKING:
    from pathlib import Path

    from ...storage.render import RenderStore

log = logging.getLogger(__name__)

#: Telegram caption hard limit (photos use a smaller cap than documents).
_CAPTION_LIMIT = 1024


class SendPhotoArgs(BaseModel):
    chat_id: int = Field(
        description=(
            "Numeric Telegram chat id (e.g. -1001234567890 for a group, a "
            "positive int for a DM). Not an @username."
        )
    )
    path: str = Field(
        description=(
            "Relative path under data/renders/ — typically the value "
            "returned by render_html. No '..', no absolute paths."
        ),
    )
    caption: str | None = Field(
        default=None,
        max_length=_CAPTION_LIMIT,
        description="Optional plain-text caption shown under the photo (max 1024 chars).",
    )
    reply_to_message_id: int | None = Field(
        default=None,
        description=(
            "Optional. Quote-reply the photo to this message id; omit for a "
            "standalone send."
        ),
    )


class TelegramSendPhotoTool(BaseTool[SendPhotoArgs]):
    name = "telegram_send_photo"
    description = (
        "Deliver a rendered image (from data/renders/, e.g. the path returned "
        "by render_html or render_latex) to a chat as an inline Telegram photo "
        "with preview. Use for tables/charts/math that Telegram markdown can't "
        "show. For an arbitrary file as a download use "
        "telegram_send_memory_document; for plain text use "
        "telegram_send_message. Path-locked to the renders root; sends "
        "immediately."
    )
    args_model = SendPhotoArgs

    async def run(self, args: SendPhotoArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        store = self.ctx.render_store
        if store is None:
            return ToolResult(content="render store unavailable", is_error=True)

        resolved = await _resolve_render(store, args.path)
        if isinstance(resolved, ToolResult):
            return resolved

        sent = await self.ctx.bot.send_photo(
            chat_id=args.chat_id,
            photo=resolved,
            caption=args.caption,
            reply_to_message_id=args.reply_to_message_id,
        )
        message_id = sent.message_id
        log.info(
            "hot-path stage=delivered chat=%s msg=%s photo=%s",
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


def _build_result(args: SendPhotoArgs, message_id: int, filename: str) -> ToolResult:
    """Assemble the success result for a delivered photo."""
    return ToolResult(
        content=f"sent photo message_id={message_id} ({filename})",
        data={
            "message_id": message_id,
            "chat_id": args.chat_id,
            "filename": filename,
            "path": args.path,
        },
    )


async def _resolve_render(store: RenderStore, path: str) -> Path | ToolResult:
    """Resolve ``path`` under the renders root.

    Returns the resolved path on success, or an error ``ToolResult`` when the
    path is rejected by the store's safety checks or points at a missing file.
    """
    try:
        resolved = await asyncio.to_thread(store.resolve_path, path)
    except Exception as exc:
        return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
    if not resolved.exists() or not resolved.is_file():
        return ToolResult(content=f"render not found: {path}", is_error=True)
    return resolved


def _transcript_text(path: str, caption: str | None) -> str:
    """Render the transcript line for a delivered photo, with optional caption."""
    if caption:
        return f"[photo] {path} — {caption}"
    return f"[photo] {path}"
