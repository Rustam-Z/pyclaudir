"""``telegram_delete_message`` — delete a Telegram message."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...db.messages import mark_deleted
from ...transcript import log_delete
from ..base import BaseTool, ToolResult


class DeleteMessageArgs(BaseModel):
    chat_id: int = Field(
        description=(
            "Numeric Telegram chat id (e.g. -1001234567890 for a group, a "
            "positive int for a DM). Not an @username."
        )
    )
    message_id: int = Field(description="Numeric id of the message to delete.")


class TelegramDeleteMessageTool(BaseTool[DeleteMessageArgs]):
    name = "telegram_delete_message"
    description = (
        "Permanently delete a message by id. IRREVERSIBLE — the message is "
        "gone for everyone, with no undo. Use sparingly (e.g. removing a "
        "mistaken send); do NOT use to 'take back' content the user has "
        "already read. Bots can only delete fairly recent messages."
    )
    args_model = DeleteMessageArgs

    async def run(self, args: DeleteMessageArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        await self.ctx.bot.delete_message(
            chat_id=args.chat_id, message_id=args.message_id
        )
        log_delete(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=args.message_id,
        )
        if self.ctx.database is not None:
            await mark_deleted(self.ctx.database, args.chat_id, args.message_id)
        return ToolResult(content=f"deleted message_id={args.message_id}")
