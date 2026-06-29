"""Restored-context digest for fresh CC sessions.

When hamroh resets the CC session (API-error auto-reset, stale-session
recovery, owner /reset_session) the model loses its conversation context.
This module builds a small, sanitized digest of recent messages from our
own SQLite history, rendered as a ``<restored_context reason="...">``
block that the engine prepends to the first turn of the fresh session.

Defensive by design — the old session may contain content that breaks a
new one: per-message truncation, lone-surrogate stripping (a lone
surrogate makes ``CcWorker.send``'s ``line.encode("utf-8")`` raise),
control-char stripping, and a ``<note>`` pointing the model at
``database_query`` for anything older than the digest.
"""

from __future__ import annotations

import re
import xml.sax.saxutils as sx
from typing import TYPE_CHECKING

from ..db.messages import RecentMessagesQuery, fetch_recent_messages
from .format import _attr

if TYPE_CHECKING:  # pragma: no cover
    from ..db.database import Database

#: How many history messages the digest carries.
DIGEST_MESSAGE_LIMIT = 10
#: Per-message character cap, applied before XML escaping.
DIGEST_TEXT_CAP = 500

#: Lone surrogates plus C0/C1 controls and DEL, keeping tab (x09) and
#: newline (x0a). Deliberately a NON-raw string — in a raw string
#: ``\uXXXX`` is not a Python escape.
_UNSAFE_CHARS = re.compile("[\ud800-\udfff\x00-\x08\x0b-\x1f\x7f-\x9f]")

_NOTE = (
    "  <note>Session was reset (reason above). These are truncated "
    "historical messages — do not reply to them. Older/full history is "
    "available via the database_query tool (messages table). Memory files are "
    "intact.</note>"
)


def sanitize_for_cc(text: str, *, cap: int = DIGEST_TEXT_CAP) -> str:
    """Strip pipe-breaking characters and truncate to ``cap`` chars.

    Lone surrogates would crash ``CcWorker.send`` at ``encode("utf-8")``;
    C0/C1 controls (except tab/newline) can corrupt the stream-json
    channel. Truncation appends an ellipsis. Applied to raw text BEFORE
    XML escaping.
    """
    cleaned = _UNSAFE_CHARS.sub("", text)
    if len(cleaned) > cap:
        cleaned = cleaned[:cap].rstrip() + "…"
    return cleaned


def _digest_entry(row: dict) -> str:
    """Render one history row as an indented ``<history_msg>`` element."""
    name = row["first_name"] or row["username"] or str(row["user_id"])
    body = sx.escape(sanitize_for_cc(row["text"] or ""))
    return (
        f'  <history_msg id="{row["message_id"]}" chat="{row["chat_id"]}" '
        f'user="{row["user_id"]}" name="{_attr(sanitize_for_cc(name))}" '
        f'direction="{row["direction"]}" time="{row["timestamp"]}">'
        f"{body}</history_msg>"
    )


async def build_restored_context(
    db: "Database | None",
    *,
    reason: str,
) -> str | None:
    """Build the ``<restored_context>`` block for the first post-reset turn.

    Returns ``None`` when there is no database or no eligible history —
    the caller then sends a plain fresh-session turn. Only committed
    rows (``processed=1``, set on clean turn completion) are eligible,
    so messages from failed turns never re-enter a fresh session.
    """
    if db is None:
        return None
    rows = await fetch_recent_messages(
        db, RecentMessagesQuery(limit=DIGEST_MESSAGE_LIMIT)
    )
    if not rows:
        return None
    parts = [f"<restored_context reason={sx.quoteattr(reason)}>"]
    parts.extend(_digest_entry(r) for r in rows)
    parts.append(_NOTE)
    parts.append("</restored_context>")
    return "\n".join(parts)
