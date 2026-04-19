"""``add_reaction`` — add an emoji reaction to a Telegram message."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..db.messages import add_bot_reaction
from ..rate_limiter import RateLimitExceeded
from ..transcript import log_reaction
from .base import BaseTool, ToolResult, handle_rate_limit


class AddReactionArgs(BaseModel):
    chat_id: int
    message_id: int
    emoji: str = Field(description="A single emoji such as 👍 or ❤️.")


class AddReactionTool(BaseTool):
    name = "add_reaction"
    description = "React to a Telegram message with a single emoji."
    args_model = AddReactionArgs

    async def run(self, args: AddReactionArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        if self.ctx.rate_limiter is not None:
            try:
                await self.ctx.rate_limiter.check_and_record(args.chat_id)
            except RateLimitExceeded as exc:
                return await handle_rate_limit(self.ctx, args.chat_id, exc)
        from telegram import ReactionTypeEmoji

        await self.ctx.bot.set_message_reaction(
            chat_id=args.chat_id,
            message_id=args.message_id,
            reaction=[ReactionTypeEmoji(emoji=args.emoji)],
        )
        log_reaction(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=args.message_id,
            emoji=args.emoji,
        )
        if self.ctx.database is not None:
            bot_id = 0
            try:
                me = await self.ctx.bot.get_me()
                bot_id = me.id
            except Exception:
                pass
            await add_bot_reaction(
                self.ctx.database,
                chat_id=args.chat_id,
                message_id=args.message_id,
                bot_user_id=bot_id,
                emoji=args.emoji,
            )
        return ToolResult(content=f"reacted {args.emoji} to {args.message_id}")
