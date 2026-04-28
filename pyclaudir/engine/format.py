"""XML formatters for batches of inbound messages.

Pure / synchronous helpers and one async formatter that walks reply
chains via the database. Used by :class:`pyclaudir.engine.Engine` and
exercised directly by ``tests/test_reply_chain.py``.
"""

from __future__ import annotations

import logging
import xml.sax.saxutils as sx
from datetime import datetime
from typing import TYPE_CHECKING

from ..db.messages import fetch_reply_chain
from ..models import ChatMessage

if TYPE_CHECKING:  # pragma: no cover
    from ..db.database import Database

log = logging.getLogger("pyclaudir.engine")

#: How many hops to walk back through a Telegram reply chain.
DEFAULT_REPLY_DEPTH = 3


def _attr(value: str) -> str:
    """XML attribute value escape that returns the inner string only."""
    return sx.quoteattr(value)[1:-1]


def _format_one(message: ChatMessage, parents_xml: str = "") -> str:
    ts = (
        message.timestamp.strftime("%H:%M")
        if isinstance(message.timestamp, datetime)
        else str(message.timestamp)
    )
    name = message.first_name or message.username or str(message.user_id)
    body = sx.escape(message.text)
    reply_attr = (
        f' reply_to="{message.reply_to_id}"' if message.reply_to_id is not None else ""
    )
    # Surface input-normalization flags to the model so it can refuse
    # obfuscated requests on-character (see system.md §Prompt-injection).
    flags_attr = (
        f' flags="{",".join(sorted(message.input_flags))}"'
        if message.input_flags
        else ""
    )
    return (
        f'<msg id="{message.message_id}" chat="{message.chat_id}" '
        f'user="{message.user_id}" name="{_attr(name)}" '
        f'time="{ts}"{reply_attr}{flags_attr}>\n'
        f"{parents_xml}{body}\n</msg>"
    )


def format_messages_as_xml(messages: list[ChatMessage]) -> str:
    """Render a batch of messages as the Claudir-style ``<msg>`` XML.

    Pure / synchronous: no DB lookup, no reply-chain expansion. Used by
    tests and as a fallback when no database is wired.
    """
    return "\n".join(_format_one(m) for m in messages)


async def format_messages_with_context(
    messages: list[ChatMessage],
    db: "Database | None",
    *,
    max_depth: int = DEFAULT_REPLY_DEPTH,
) -> str:
    """Render a batch of messages with reply-chain context expanded.

    For every message in ``messages`` whose ``reply_to_id`` is set, walk our
    own ``messages`` table back up to ``max_depth`` hops and embed each
    parent inside the rendered ``<msg>`` block as ``<reply_chain><parent
    .../></reply_chain>``.

    Lookup misses fall back to the inline ``reply_to_text`` Telegram echoed
    in the original update (if present), so the model still sees something
    when our DB doesn't have the parent — e.g. the bot was just added to
    the group and the user immediately replied to a pre-existing message.

    If ``db`` is ``None`` we degrade to the pure formatter — same path as
    :func:`format_messages_as_xml`.
    """
    if db is None:
        return format_messages_as_xml(messages)

    rendered: list[str] = []
    for m in messages:
        parents_xml = ""
        if m.reply_to_id is not None:
            try:
                chain = await fetch_reply_chain(
                    db, m.chat_id, m.reply_to_id, max_depth=max_depth
                )
            except Exception:  # pragma: no cover
                log.exception("reply chain lookup failed for %s", m.message_id)
                chain = []

            if chain:
                parts: list[str] = ["<reply_chain>"]
                for p in chain:
                    pname = p["first_name"] or p["username"] or str(p["user_id"])
                    parts.append(
                        f'  <parent id="{p["message_id"]}" user="{p["user_id"]}" '
                        f'name="{_attr(pname)}" direction="{p["direction"]}" '
                        f'time="{p["timestamp"]}">'
                        f"{sx.escape(p['text'] or '')}"
                        f"</parent>"
                    )
                parts.append("</reply_chain>\n")
                parents_xml = "\n".join(parts)
            elif m.reply_to_text:
                # DB miss — fall back to whatever Telegram inlined.
                parents_xml = (
                    "<reply_chain>\n"
                    f'  <parent id="{m.reply_to_id}" source="telegram_inline">'
                    f"{sx.escape(m.reply_to_text)}</parent>\n"
                    "</reply_chain>\n"
                )
        rendered.append(_format_one(m, parents_xml))
    return "\n".join(rendered)
