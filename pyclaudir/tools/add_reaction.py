"""``add_reaction`` — add an emoji reaction to a Telegram message."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..db.messages import add_bot_reaction
from ..transcript import log_reaction
from .base import BaseTool, ToolResult


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
        from telegram import ReactionTypeEmoji

        await self.ctx.bot.set_message_reaction(
            chat_id=args.chat_id,
            message_id=args.message_id,
            reaction=[ReactionTypeEmoji(emoji=args.emoji)],
        )

        # Stop the "typing..." indicator: the reaction is visible feedback,
        # no need to keep refreshing typing until turn-end.
        if self.ctx.on_chat_replied is not None:
            try:
                self.ctx.on_chat_replied(args.chat_id)
            except Exception:  # pragma: no cover
                pass

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
