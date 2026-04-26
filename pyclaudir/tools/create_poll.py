"""``create_poll`` — send a Telegram poll (regular or quiz)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..db.messages import insert_message
from ..models import ChatMessage
from ..transcript import log_outbound
from .base import BaseTool, ToolResult

log = logging.getLogger(__name__)


class CreatePollArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id.")
    question: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=10)
    is_anonymous: bool = True
    type: Literal["regular", "quiz"] = "regular"
    allows_multiple_answers: bool = False
    correct_option_id: int | None = Field(
        default=None,
        ge=0,
        le=9,
        description="0-based index of the correct option. Required for quizzes.",
    )
    explanation: str | None = Field(
        default=None,
        max_length=200,
        description="Shown when a quiz answer is wrong. Quiz only.",
    )
    open_period: int | None = Field(
        default=None,
        ge=5,
        le=600,
        description="Auto-close after N seconds (5-600). Mutually exclusive with close_date.",
    )
    close_date: int | None = Field(
        default=None,
        description="Unix timestamp 5-600s in the future. Mutually exclusive with open_period.",
    )
    reply_to_message_id: int | None = None

    @model_validator(mode="after")
    def _validate(self) -> "CreatePollArgs":
        for i, opt in enumerate(self.options):
            if not (1 <= len(opt) <= 100):
                raise ValueError(f"option {i} must be 1-100 chars")
        if self.type == "quiz":
            if self.correct_option_id is None:
                raise ValueError("correct_option_id is required when type='quiz'")
            if self.allows_multiple_answers:
                raise ValueError("allows_multiple_answers is only valid for regular polls")
        else:
            if self.correct_option_id is not None:
                raise ValueError("correct_option_id is only valid when type='quiz'")
            if self.explanation is not None:
                raise ValueError("explanation is only valid when type='quiz'")
        if self.correct_option_id is not None and self.correct_option_id >= len(self.options):
            raise ValueError("correct_option_id is out of range")
        if self.open_period is not None and self.close_date is not None:
            raise ValueError("open_period and close_date are mutually exclusive")
        return self


class CreatePollTool(BaseTool):
    name = "create_poll"
    description = (
        "Send a Telegram poll. Supports regular polls, quizzes (with correct "
        "answer + explanation), multi-answer, non-anonymous voting, an auto-close "
        "timer, and reply-to. Returns the new message_id and poll_id."
    )
    args_model = CreatePollArgs

    async def run(self, args: CreatePollArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)

        sent = await self.ctx.bot.send_poll(
            chat_id=args.chat_id,
            question=args.question,
            options=args.options,
            is_anonymous=args.is_anonymous,
            type=args.type,
            allows_multiple_answers=args.allows_multiple_answers,
            correct_option_id=args.correct_option_id,
            explanation=args.explanation,
            open_period=args.open_period,
            close_date=args.close_date,
            reply_to_message_id=args.reply_to_message_id,
        )
        message_id = sent.message_id
        poll_id = sent.poll.id if sent.poll is not None else None
        log.info(
            "hot-path stage=delivered chat=%s msg=%s poll=%s",
            args.chat_id, message_id, poll_id,
        )

        if self.ctx.on_chat_replied is not None:
            try:
                self.ctx.on_chat_replied(args.chat_id)
            except Exception:  # pragma: no cover
                pass

        transcript_text = f"[poll] {args.question}"
        log_outbound(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=message_id,
            reply_to_id=args.reply_to_message_id,
            text=transcript_text,
        )

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
            stored_text = transcript_text + "\n" + "\n".join(
                f"- {opt}" for opt in args.options
            )
            await insert_message(
                self.ctx.database,
                ChatMessage(
                    chat_id=args.chat_id,
                    message_id=message_id,
                    user_id=bot_user_id,
                    username=bot_username,
                    first_name=bot_first_name,
                    direction="out",
                    timestamp=datetime.now(timezone.utc),
                    text=stored_text,
                    reply_to_id=args.reply_to_message_id,
                ),
            )

        return ToolResult(
            content=f"poll sent message_id={message_id} poll_id={poll_id}",
            data={
                "message_id": message_id,
                "poll_id": poll_id,
                "chat_id": args.chat_id,
            },
        )
