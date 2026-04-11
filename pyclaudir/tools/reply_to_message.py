"""``reply_to_message`` — convenience wrapper that *requires* a reply target."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult
from .send_message import SendMessageArgs, SendMessageTool


class ReplyToMessageArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id.")
    reply_to_message_id: int = Field(description="The message_id to quote-reply.")
    text: str = Field(description="Reply body.")


class ReplyToMessageTool(BaseTool):
    name = "reply_to_message"
    description = (
        "Send a quote-reply to a specific message. Use this when the chat is "
        "active and your reply needs to be unambiguously tied to one message."
    )
    args_model = ReplyToMessageArgs

    async def run(self, args: ReplyToMessageArgs) -> ToolResult:
        # Reuse the send_message tool's logic verbatim, including rate limit
        # and persistence.
        delegate = SendMessageTool(self.ctx)
        return await delegate.run(
            SendMessageArgs(
                chat_id=args.chat_id,
                text=args.text,
                reply_to_message_id=args.reply_to_message_id,
            )
        )
