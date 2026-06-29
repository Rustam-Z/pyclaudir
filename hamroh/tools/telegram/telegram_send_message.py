"""``telegram_send_message`` — The agent's primary way to talk to humans."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from ...formatting import chunk_text, markdown_to_telegram_html
from ...transcript import ChatRef, MsgRef, log_outbound
from ..base import (
    BaseTool,
    OutboundRecord,
    ToolResult,
    notify_chat_replied,
    record_outbound,
)

log = logging.getLogger(__name__)


class SendMessageArgs(BaseModel):
    chat_id: int = Field(
        description=(
            "Numeric Telegram chat id (e.g. -1001234567890 for a group, a "
            "positive int for a DM). Not an @username."
        )
    )
    text: str = Field(
        description=(
            "Message body. Markdown by default — auto-converted to Telegram "
            "HTML. Sent verbatim when parse_mode is set."
        )
    )
    reply_to_message_id: int | None = Field(
        default=None,
        description=(
            "Optional. Make this a quote-reply to the given message id. When a "
            "reply target is required, prefer telegram_reply_to_message."
        ),
    )
    parse_mode: Literal["HTML", "MarkdownV2", None] = Field(
        default=None,
        description=(
            "Optional. Leave null for normal markdown text (auto-converted to "
            "HTML). Set 'HTML' or 'MarkdownV2' only to send pre-formatted "
            "content verbatim."
        ),
    )


class TelegramSendMessageTool(BaseTool[SendMessageArgs]):
    name = "telegram_send_message"
    description = (
        "Send a NEW text message to a Telegram chat — the primary way to "
        "deliver text to the user. Use for any standalone reply. Does NOT edit "
        "or quote existing messages (use telegram_edit_message / "
        "telegram_reply_to_message) and does NOT send images or files (use "
        "telegram_send_photo / telegram_send_memory_document). Sends "
        "immediately and cannot be unsent. Long text is auto-split at "
        "paragraph boundaries; returns the first message_id plus every id in "
        "``message_ids``."
    )
    args_model = SendMessageArgs

    async def run(self, args: SendMessageArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)

        # Chunk the RAW text before markdown conversion so each chunk's HTML
        # is self-contained (no mid-tag splits across chunk boundaries).
        raw_chunks = chunk_text(args.text)
        bodies, parse_mode = _render_bodies(raw_chunks, args.parse_mode)

        message_ids = await self._deliver_chunks(args, bodies, parse_mode)
        await self._persist_chunks(args, message_ids, raw_chunks)
        return _build_result(message_ids, args.chat_id)

    async def _deliver_chunks(
        self,
        args: SendMessageArgs,
        bodies: list[str],
        parse_mode: Literal["HTML", "MarkdownV2", None],
    ) -> list[int]:
        """Send each chunk in order, returning the delivered message ids."""
        assert self.ctx.bot is not None  # guarded by caller
        message_ids: list[int] = []
        for i, body in enumerate(bodies):
            reply_to = args.reply_to_message_id if i == 0 else None
            sent = await self.ctx.bot.send_message(
                chat_id=args.chat_id,
                text=body,
                reply_to_message_id=reply_to,
                parse_mode=parse_mode,
            )
            message_ids.append(sent.message_id)
            log.info(
                "hot-path stage=delivered chat=%s msg=%s chunk=%d/%d",
                args.chat_id,
                sent.message_id,
                i + 1,
                len(bodies),
            )

            # Stop typing after the FIRST chunk lands — user has visible
            # content. Subsequent chunks stream in without the indicator.
            if i == 0:
                notify_chat_replied(self.ctx, args.chat_id)
        return message_ids

    async def _persist_chunks(
        self,
        args: SendMessageArgs,
        message_ids: list[int],
        raw_chunks: list[str],
    ) -> None:
        """Log + persist each delivered chunk as its own transcript row.

        ``record_outbound`` internally handles the bot-identity lookup; PTB
        caches the ``get_me`` result after the first call, so the N-1
        follow-ups cost a dict read.
        """
        log_outbound(
            ChatRef(args.chat_id, self.ctx.chat_titles),
            MsgRef(message_ids[0], args.text, args.reply_to_message_id),
        )
        for i, (mid, raw_chunk) in enumerate(zip(message_ids, raw_chunks)):
            await record_outbound(
                self.ctx,
                OutboundRecord(
                    args.chat_id,
                    mid,
                    raw_chunk,
                    args.reply_to_message_id if i == 0 else None,
                ),
            )


def _render_bodies(
    raw_chunks: list[str],
    parse_mode: Literal["HTML", "MarkdownV2", None],
) -> tuple[list[str], Literal["HTML", "MarkdownV2", None]]:
    """Build the per-chunk message bodies and the effective parse mode.

    With no parse mode the markdown chunks are converted to HTML; otherwise
    the caller owns formatting and the chunks are sent verbatim.
    """
    if parse_mode is None:
        return [markdown_to_telegram_html(c) for c in raw_chunks], "HTML"
    return list(raw_chunks), parse_mode


def _build_result(message_ids: list[int], chat_id: int) -> ToolResult:
    """Assemble the tool result from the delivered message ids."""
    first_id = message_ids[0]
    content = (
        f"sent message_id={first_id}"
        if len(message_ids) == 1
        else f"sent {len(message_ids)} chunks: message_ids={message_ids}"
    )
    return ToolResult(
        content=content,
        data={
            "message_id": first_id,
            "message_ids": message_ids,
            "chat_id": chat_id,
        },
    )
