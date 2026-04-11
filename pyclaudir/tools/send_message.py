"""``send_message`` — Nodira's primary way to talk to humans."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from ..db.messages import insert_message
from ..models import ChatMessage
from ..rate_limiter import RateLimitExceeded
from ..transcript import log_outbound
from .base import BaseTool, ToolResult


class SendMessageArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id.")
    text: str = Field(description="Message body. Plain text by default.")
    reply_to_message_id: int | None = Field(default=None)
    parse_mode: Literal["HTML", "MarkdownV2", None] = Field(default=None)


class SendMessageTool(BaseTool):
    name = "send_message"
    description = (
        "Send a text message to a Telegram chat. Returns the new message_id. "
        "Rate-limited to 20 messages per minute per chat."
    )
    args_model = SendMessageArgs

    async def run(self, args: SendMessageArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        if self.ctx.rate_limiter is not None:
            try:
                self.ctx.rate_limiter.check_and_record(args.chat_id)
            except RateLimitExceeded as exc:
                return ToolResult(content=str(exc), is_error=True)

        sent = await self.ctx.bot.send_message(
            chat_id=args.chat_id,
            text=args.text,
            reply_to_message_id=args.reply_to_message_id,
            parse_mode=args.parse_mode,
        )

        log_outbound(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=sent.message_id,
            reply_to_id=args.reply_to_message_id,
            text=args.text,
        )

        # Persist *after* delivery is confirmed (per the spec's "don't put
        # outbound on the queue until send confirms").
        if self.ctx.database is not None:
            try:
                me = await self.ctx.bot.get_me()
                bot_user_id = me.id
                bot_username = me.username
                bot_first_name = me.first_name
            except Exception:
                bot_user_id = 0
                bot_username = None
                bot_first_name = "nodira"
            await insert_message(
                self.ctx.database,
                ChatMessage(
                    chat_id=args.chat_id,
                    message_id=sent.message_id,
                    user_id=bot_user_id,
                    username=bot_username,
                    first_name=bot_first_name,
                    direction="out",
                    timestamp=datetime.now(timezone.utc),
                    text=args.text,
                    reply_to_id=args.reply_to_message_id,
                ),
            )

        return ToolResult(
            content=f"sent message_id={sent.message_id}",
            data={"message_id": sent.message_id, "chat_id": args.chat_id},
        )
