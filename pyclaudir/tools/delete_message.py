"""``delete_message`` — delete a Telegram message."""

from __future__ import annotations

from pydantic import BaseModel

from ..db.messages import mark_deleted
from ..transcript import log_delete
from .base import BaseTool, ToolResult


class DeleteMessageArgs(BaseModel):
    chat_id: int
    message_id: int


class DeleteMessageTool(BaseTool):
    name = "delete_message"
    description = "Delete a Telegram message by id. Bots can only delete recent messages."
    args_model = DeleteMessageArgs

    async def run(self, args: DeleteMessageArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        await self.ctx.bot.delete_message(chat_id=args.chat_id, message_id=args.message_id)

        # Stop the "typing..." indicator: the delete has landed in Telegram.
        if self.ctx.on_chat_replied is not None:
            try:
                self.ctx.on_chat_replied(args.chat_id)
            except Exception:  # pragma: no cover
                pass

        log_delete(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=args.message_id,
        )
        if self.ctx.database is not None:
            await mark_deleted(self.ctx.database, args.chat_id, args.message_id)
        return ToolResult(content=f"deleted message_id={args.message_id}")
