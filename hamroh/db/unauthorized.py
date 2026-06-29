"""Persistence helpers for the ``unauthorized_messages`` table.

Inbound messages from chats that fail the access gate are logged here
and only here — never in the main ``messages`` table, and never in
``users``. Kept separate from ``messages.py`` so the main message
helpers don't drift past the file-length limit and so the trash-table
concern stays isolated by domain.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .database import Database
from ..models import ChatMessage


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


#: Rows older than this are dropped on every insert — strangers DMing the
#: bot must not grow the table without bound.
RETENTION_DAYS = 30


async def insert_unauthorized_message(
    db: Database,
    *,
    cm: ChatMessage,
    chat_type: str | None,
    refusal_sent: bool,
) -> None:
    """Log an inbound message that failed the access gate.

    ``refusal_sent`` is ``True`` exactly on the row whose arrival
    triggered the one-time "private assistant" reply. Each insert also
    prunes rows older than :data:`RETENTION_DAYS`.
    """
    await db.execute(
        "DELETE FROM unauthorized_messages "
        f"WHERE timestamp < datetime('now', '-{RETENTION_DAYS} days')",
    )
    await db.execute(
        """
        INSERT INTO unauthorized_messages
            (chat_id, chat_type, message_id, user_id, username, first_name,
             timestamp, text, refusal_sent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cm.chat_id,
            chat_type,
            cm.message_id,
            cm.user_id,
            cm.username,
            cm.first_name,
            _iso(cm.timestamp),
            cm.text,
            1 if refusal_sent else 0,
        ),
    )


async def chat_has_refusal(db: Database, chat_id: int) -> bool:
    """Return True if the one-time refusal reply has already been sent
    in this chat."""
    row = await db.fetch_one(
        "SELECT 1 FROM unauthorized_messages WHERE chat_id=? AND refusal_sent=1 LIMIT 1",
        (chat_id,),
    )
    return row is not None
