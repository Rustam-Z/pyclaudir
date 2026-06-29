"""``telegram_edit_message`` — edit a message the bot previously sent."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...db.messages import mark_edited
from ...formatting import markdown_to_telegram_html
from ...transcript import log_edit
from ..base import BaseTool, ToolResult


class EditMessageArgs(BaseModel):
    chat_id: int = Field(
        description=(
            "Numeric Telegram chat id (e.g. -1001234567890 for a group, a "
            "positive int for a DM). Not an @username."
        )
    )
    message_id: int = Field(
        description=(
            "Numeric id of the bot's own message to edit (e.g. a message_id "
            "returned by telegram_send_message)."
        )
    )
    text: str = Field(
        description=(
            "New body, replacing the old text entirely. Markdown by default — "
            "auto-converted to Telegram HTML."
        )
    )


class TelegramEditMessageTool(BaseTool[EditMessageArgs]):
    name = "telegram_edit_message"
    description = (
        "Replace the text of a message THIS BOT already sent. Use for "
        "in-progress updates on a long task — edits do NOT trigger a Telegram "
        "push notification, unlike a new message. Only works on the bot's own "
        "recent messages; to post fresh text use telegram_send_message."
    )
    args_model = EditMessageArgs

    async def run(self, args: EditMessageArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        text = markdown_to_telegram_html(args.text)
        await self.ctx.bot.edit_message_text(
            chat_id=args.chat_id,
            message_id=args.message_id,
            text=text,
            parse_mode="HTML",
        )
        log_edit(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=args.message_id,
            text=args.text,
        )
        if self.ctx.database is not None:
            await mark_edited(
                self.ctx.database, args.chat_id, args.message_id, args.text
            )
        return ToolResult(
            content=f"edited message_id={args.message_id}",
            data={"message_id": args.message_id, "chat_id": args.chat_id},
        )
