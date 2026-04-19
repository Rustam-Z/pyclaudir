"""``delete_message`` — delete a Telegram message."""

from __future__ import annotations

from pydantic import BaseModel

from ..db.messages import mark_deleted
from ..rate_limiter import RateLimitExceeded
from ..transcript import log_delete
from .base import BaseTool, ToolResult, handle_rate_limit


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
        if self.ctx.rate_limiter is not None:
            try:
                await self.ctx.rate_limiter.check_and_record(args.chat_id)
            except RateLimitExceeded as exc:
                return await handle_rate_limit(self.ctx, args.chat_id, exc)
        await self.ctx.bot.delete_message(chat_id=args.chat_id, message_id=args.message_id)
        log_delete(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=args.message_id,
        )
        if self.ctx.database is not None:
            await mark_deleted(self.ctx.database, args.chat_id, args.message_id)
        return ToolResult(content=f"deleted message_id={args.message_id}")
