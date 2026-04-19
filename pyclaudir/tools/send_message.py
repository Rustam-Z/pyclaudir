"""``send_message`` — The agent's primary way to talk to humans."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from ..db.messages import insert_message
from ..formatting import markdown_to_telegram_html
from ..models import ChatMessage
from ..transcript import log_outbound
from .base import BaseTool, ToolResult


class SendMessageArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id.")
    text: str = Field(description="Message body. Plain text by default.")
    reply_to_message_id: int | None = Field(default=None)
    parse_mode: Literal["HTML", "MarkdownV2", None] = Field(default=None)


class SendMessageTool(BaseTool):
    name = "send_message"
    description = "Send a text message to a Telegram chat. Returns the new message_id."
    args_model = SendMessageArgs

    async def run(self, args: SendMessageArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)

        # Auto-convert markdown to Telegram HTML when no explicit parse_mode
        # is requested. This handles the common case where the LLM produces
        # markdown (bold, links, code) that Telegram can't render as-is.
        text = args.text
        parse_mode = args.parse_mode
        if parse_mode is None:
            text = markdown_to_telegram_html(text)
            parse_mode = "HTML"

        sent = await self.ctx.bot.send_message(
            chat_id=args.chat_id,
            text=text,
            reply_to_message_id=args.reply_to_message_id,
            parse_mode=parse_mode,
        )

        # Notify the engine immediately so the "typing..." indicator can
        # stop refreshing. We do this *before* persistence + transcript
        # because those are local-only and the user is staring at their
        # phone right now.
        if self.ctx.on_chat_replied is not None:
            try:
                self.ctx.on_chat_replied(args.chat_id)
            except Exception:  # pragma: no cover
                pass

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
                bot_first_name = "bot"
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
