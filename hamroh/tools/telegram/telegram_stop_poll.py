"""``telegram_stop_poll`` — close a Telegram poll and return final tallies."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult

log = logging.getLogger(__name__)


class StopPollArgs(BaseModel):
    chat_id: int = Field(
        description="Numeric Telegram chat id where the poll was sent."
    )
    message_id: int = Field(description="Numeric message_id of the poll to close.")


class TelegramStopPollTool(BaseTool[StopPollArgs]):
    name = "telegram_stop_poll"
    description = (
        "Close an open Telegram poll early and return the final vote tallies. "
        "Use the chat_id and message_id returned by the telegram_create_poll "
        "that created it. The poll stops accepting votes immediately."
    )
    args_model = StopPollArgs

    async def run(self, args: StopPollArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)

        poll = await self.ctx.bot.stop_poll(
            chat_id=args.chat_id,
            message_id=args.message_id,
        )
        log.info(
            "poll closed chat=%s msg=%s votes=%d",
            args.chat_id,
            args.message_id,
            poll.total_voter_count,
        )

        options_data = [
            {"text": o.text, "voter_count": o.voter_count} for o in poll.options
        ]
        lines = [f"poll closed: {poll.total_voter_count} votes"]
        lines.extend(f"- {o['text']}: {o['voter_count']}" for o in options_data)
        content = "\n".join(lines)

        return ToolResult(
            content=content,
            data={
                "poll_id": poll.id,
                "total_voter_count": poll.total_voter_count,
                "options": options_data,
                "is_closed": poll.is_closed,
                "chat_id": args.chat_id,
                "message_id": args.message_id,
            },
        )
