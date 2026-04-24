"""``send_message_draft`` — stream a progress preview into a Telegram draft bubble.

Backed by Telegram Bot API 9.5's ``sendMessageDraft`` (March 2026), which
animates partial message text in a dedicated bubble identified by
``draft_id``. Repeat the same ``draft_id`` to update the same bubble
natively (no "edited" tag, no per-update notification). Drafts are
ephemeral — callers must still use ``send_message`` for the persisted
reply.

Unlike :mod:`send_message` this tool does **not** chunk, does **not**
persist to the DB, and does **not** convert markdown. It's a lightweight
progress indicator: one call, one bubble update, up to 4096 chars. If
you're tempted to stream 20k of prose through here, you've outgrown the
use case — formulate the full reply and send it via ``send_message``.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult

log = logging.getLogger(__name__)

#: Same per-message cap ``sendMessage`` uses — the draft method's docs
#: quote "1-4096 characters after entities parsing".
_TELEGRAM_TEXT_LIMIT = 4096


def _auto_draft_id(chat_id: int) -> int:
    """Stable non-zero 31-bit int for chats that don't supply their own.

    Telegram animates updates that share a ``draft_id``. We mix the
    chat_id with ``time.time_ns()`` so repeat calls from the same turn
    that omit ``draft_id`` each start a fresh bubble — if the caller
    wants in-place animation they must capture the returned id and pass
    it on subsequent calls.
    """
    mixed = hash((chat_id, time.time_ns())) & 0x7FFFFFFF
    return mixed or 1


class SendMessageDraftArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id.")
    text: str = Field(
        description=(
            "Draft/progress text. 1-4096 chars (no auto-chunking — if you "
            "need more, you're past the 'preview' use case; use send_message)."
        ),
        min_length=1,
        max_length=_TELEGRAM_TEXT_LIMIT,
    )
    draft_id: int | None = Field(
        default=None,
        description=(
            "Non-zero integer. Reuse the returned draft_id on subsequent "
            "calls to animate the SAME bubble; pass a new one (or omit) "
            "to start a fresh bubble."
        ),
    )
    parse_mode: Literal["HTML", "MarkdownV2", None] = Field(
        default=None,
        description=(
            "Opt-in formatting. Plain text by default because mid-stream "
            "content is often partial markdown/HTML that renders broken."
        ),
    )


class SendMessageDraftTool(BaseTool):
    name = "send_message_draft"
    description = (
        "Stream a progress/draft preview into an animated Telegram bubble. "
        "Use during long tasks (web research, multi-step analysis) so the "
        "user sees live status instead of silent typing. Reuse the "
        "returned draft_id to update the same bubble in place. Drafts are "
        "ephemeral — always follow up with send_message to deliver the "
        "persisted final reply. Max 4096 chars per call; no auto-chunking."
    )
    args_model = SendMessageDraftArgs

    async def run(self, args: SendMessageDraftArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)

        draft_id = args.draft_id if args.draft_id else _auto_draft_id(args.chat_id)
        if draft_id == 0:
            return ToolResult(
                content="draft_id must be non-zero",
                is_error=True,
            )

        api_kwargs: dict[str, object] = {
            "chat_id": args.chat_id,
            "draft_id": draft_id,
            "text": args.text,
        }
        if args.parse_mode is not None:
            api_kwargs["parse_mode"] = args.parse_mode

        try:
            await self.ctx.bot.do_api_request(
                "sendMessageDraft", api_kwargs=api_kwargs,
            )
        except Exception as exc:
            log.warning(
                "sendMessageDraft failed chat=%s draft=%s: %s",
                args.chat_id, draft_id, exc,
            )
            return ToolResult(
                content=f"sendMessageDraft failed: {exc}",
                is_error=True,
            )

        log.info(
            "hot-path stage=draft chat=%s draft=%d len=%d",
            args.chat_id, draft_id, len(args.text),
        )

        # Dismiss typing — user now sees a draft bubble, same rationale as
        # send_message's ``on_chat_replied`` fire on first chunk.
        if self.ctx.on_chat_replied is not None:
            try:
                self.ctx.on_chat_replied(args.chat_id)
            except Exception:  # pragma: no cover
                pass

        return ToolResult(
            content=(
                f"draft updated draft_id={draft_id} "
                f"(reuse this id to animate the same bubble; "
                f"call send_message when the final reply is ready)"
            ),
            data={"draft_id": draft_id, "chat_id": args.chat_id},
        )
