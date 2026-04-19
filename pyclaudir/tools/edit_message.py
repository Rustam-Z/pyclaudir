"""``edit_message`` — edit a message the bot previously sent."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..db.messages import mark_edited
from ..formatting import markdown_to_telegram_html
from ..rate_limiter import RateLimitExceeded
from ..transcript import log_edit
from .base import BaseTool, ToolResult, handle_rate_limit


class EditMessageArgs(BaseModel):
    chat_id: int
    message_id: int
    text: str = Field(description="New message body.")


class EditMessageTool(BaseTool):
    name = "edit_message"
    description = (
        "Edit a previously sent message. Edits don't trigger Telegram push "
        "notifications, so use this for interim progress updates."
    )
    args_model = EditMessageArgs

    async def run(self, args: EditMessageArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        if self.ctx.rate_limiter is not None:
            try:
                await self.ctx.rate_limiter.check_and_record(args.chat_id)
            except RateLimitExceeded as exc:
                return await handle_rate_limit(self.ctx, args.chat_id, exc)
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
            await mark_edited(self.ctx.database, args.chat_id, args.message_id, args.text)
        return ToolResult(
            content=f"edited message_id={args.message_id}",
            data={"message_id": args.message_id, "chat_id": args.chat_id},
        )
