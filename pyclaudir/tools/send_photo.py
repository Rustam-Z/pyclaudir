"""``send_photo`` — send a rendered image to a chat as a Telegram photo.

Companion to :mod:`pyclaudir.tools.render_html`. Path is locked to the
renders root with the same hardening pattern as ``send_memory_document``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..db.messages import insert_message
from ..models import ChatMessage
from ..transcript import log_outbound
from .base import BaseTool, ToolResult

log = logging.getLogger(__name__)

#: Telegram caption hard limit (photos use a smaller cap than documents).
_CAPTION_LIMIT = 1024


class SendPhotoArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id.")
    path: str = Field(
        description=(
            "Relative path under data/renders/ — typically the value "
            "returned by render_html. No '..', no absolute paths."
        ),
    )
    caption: str | None = Field(
        default=None,
        max_length=_CAPTION_LIMIT,
        description="Optional plain-text caption shown under the photo.",
    )
    reply_to_message_id: int | None = None


class SendPhotoTool(BaseTool):
    name = "send_photo"
    description = (
        "Send a rendered photo (under data/renders/) to a Telegram chat. "
        "Use after render_html to deliver the image as an inline photo "
        "(with preview), not a document. Path-locked to the renders root."
    )
    args_model = SendPhotoArgs

    async def run(self, args: SendPhotoArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        store = self.ctx.render_store
        if store is None:
            return ToolResult(content="render store unavailable", is_error=True)

        try:
            resolved = await asyncio.to_thread(store.resolve_path, args.path)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)

        if not resolved.exists() or not resolved.is_file():
            return ToolResult(
                content=f"render not found: {args.path}", is_error=True,
            )

        sent = await self.ctx.bot.send_photo(
            chat_id=args.chat_id,
            photo=resolved,
            caption=args.caption,
            reply_to_message_id=args.reply_to_message_id,
        )
        message_id = sent.message_id
        log.info(
            "hot-path stage=delivered chat=%s msg=%s photo=%s",
            args.chat_id, message_id, args.path,
        )

        if self.ctx.on_chat_replied is not None:
            try:
                self.ctx.on_chat_replied(args.chat_id)
            except Exception:  # pragma: no cover
                pass

        transcript_text = f"[photo] {args.path}"
        if args.caption:
            transcript_text += f" — {args.caption}"
        log_outbound(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=message_id,
            reply_to_id=args.reply_to_message_id,
            text=transcript_text,
        )

        if self.ctx.database is not None:
            try:
                me = await self.ctx.bot.get_me()
                bot_user_id = me.id
                bot_username = me.username
                bot_first_name = me.first_name
            except Exception:
                bot_user_id = 0
                bot_username = None
                bot_first_name = "bot"
            await insert_message(
                self.ctx.database,
                ChatMessage(
                    chat_id=args.chat_id,
                    message_id=message_id,
                    user_id=bot_user_id,
                    username=bot_username,
                    first_name=bot_first_name,
                    direction="out",
                    timestamp=datetime.now(timezone.utc),
                    text=transcript_text,
                    reply_to_id=args.reply_to_message_id,
                ),
            )

        return ToolResult(
            content=f"sent photo message_id={message_id} ({resolved.name})",
            data={
                "message_id": message_id,
                "chat_id": args.chat_id,
                "filename": resolved.name,
                "path": args.path,
            },
        )
