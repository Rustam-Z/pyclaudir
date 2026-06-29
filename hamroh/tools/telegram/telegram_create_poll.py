"""``telegram_create_poll`` — send a Telegram poll (regular or quiz)."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..base import BaseTool, OutboundDelivery, ToolResult, deliver_bookkeeping

log = logging.getLogger(__name__)


class CreatePollArgs(BaseModel):
    chat_id: int = Field(
        description=(
            "Numeric Telegram chat id (e.g. -1001234567890 for a group, a "
            "positive int for a DM). Not an @username."
        )
    )
    question: str = Field(
        min_length=1, max_length=300, description="Poll question (1–300 chars)."
    )
    options: list[str] = Field(
        min_length=2,
        max_length=10,
        description="Answer options: 2–10 strings, each 1–100 chars.",
    )
    is_anonymous: bool = Field(
        default=True, description="Whether votes are anonymous. Default true."
    )
    type: Literal["regular", "quiz"] = Field(
        default="regular",
        description=(
            "'regular' for a normal vote, 'quiz' for a single-correct-answer "
            "quiz. Default 'regular'."
        ),
    )
    allows_multiple_answers: bool = Field(
        default=False,
        description=(
            "Let voters pick several options. Regular polls only; must be "
            "false for quizzes. Default false."
        ),
    )
    correct_option_id: int | None = Field(
        default=None,
        ge=0,
        le=9,
        description=(
            "0-based index of the correct option. Required when type='quiz'; "
            "omit for regular polls."
        ),
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
    reply_to_message_id: int | None = Field(
        default=None,
        description=(
            "Optional. Quote-reply the poll to this message id; omit for a "
            "standalone poll."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> "CreatePollArgs":
        self._check_option_lengths()
        self._check_quiz_rules()
        self._check_correct_option_range()
        self._check_close_timing()
        return self

    def _check_option_lengths(self) -> None:
        """Each answer option must be 1-100 chars."""
        for i, opt in enumerate(self.options):
            if not (1 <= len(opt) <= 100):
                raise ValueError(f"option {i} must be 1-100 chars")

    def _check_quiz_rules(self) -> None:
        """Quiz-only and regular-only fields must match the poll type."""
        if self.type == "quiz":
            if self.correct_option_id is None:
                raise ValueError("correct_option_id is required when type='quiz'")
            if self.allows_multiple_answers:
                raise ValueError(
                    "allows_multiple_answers is only valid for regular polls"
                )
            return
        if self.correct_option_id is not None:
            raise ValueError("correct_option_id is only valid when type='quiz'")
        if self.explanation is not None:
            raise ValueError("explanation is only valid when type='quiz'")

    def _check_correct_option_range(self) -> None:
        """correct_option_id must point at an existing option."""
        if self.correct_option_id is not None and self.correct_option_id >= len(
            self.options
        ):
            raise ValueError("correct_option_id is out of range")

    def _check_close_timing(self) -> None:
        """open_period and close_date cannot both be set."""
        if self.open_period is not None and self.close_date is not None:
            raise ValueError("open_period and close_date are mutually exclusive")


class TelegramCreatePollTool(BaseTool[CreatePollArgs]):
    name = "telegram_create_poll"
    description = (
        "Send a Telegram poll — a regular vote or a quiz with one correct "
        "answer. Use for structured voting; for plain text use "
        "telegram_send_message. Sends immediately. Rules: 2–10 options, each "
        "1–100 chars; for type='quiz' you MUST set correct_option_id and must "
        "NOT set allows_multiple_answers; for type='regular' omit "
        "correct_option_id and explanation; open_period and close_date are "
        "mutually exclusive. Returns message_id and poll_id (pass them to "
        "telegram_stop_poll to close the poll early)."
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
            args.chat_id,
            message_id,
            poll_id,
        )

        await self._record_delivery(args, message_id)

        return ToolResult(
            content=f"poll sent message_id={message_id} poll_id={poll_id}",
            data={
                "message_id": message_id,
                "poll_id": poll_id,
                "chat_id": args.chat_id,
            },
        )

    async def _record_delivery(self, args: CreatePollArgs, message_id: int) -> None:
        """Persist the sent poll to the transcript and database."""
        transcript_text = f"[poll] {args.question}"
        stored_text = (
            transcript_text + "\n" + "\n".join(f"- {opt}" for opt in args.options)
        )
        await deliver_bookkeeping(
            self.ctx,
            OutboundDelivery(
                chat_id=args.chat_id,
                message_id=message_id,
                reply_to_id=args.reply_to_message_id,
                transcript_text=transcript_text,
                db_text=stored_text,
            ),
        )
