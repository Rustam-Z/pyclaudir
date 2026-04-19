"""Persistence helpers for the ``messages``, ``users``, and ``tool_calls`` tables.

Kept as plain functions taking a :class:`Database` so they're trivial to mock
in tests and don't entangle the database wrapper with PTB types.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable

from .database import Database
from ..models import ChatMessage


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def insert_message(db: Database, msg: ChatMessage) -> None:
    """Idempotently insert a Telegram message row.

    Edited messages re-fire the handler with the same ``message_id``; we use
    ``INSERT OR REPLACE`` so the row stays current. The ``edited`` flag is
    bumped via :func:`mark_edited` from the edited-message handler instead.
    """
    await db.execute(
        """
        INSERT OR REPLACE INTO messages
            (chat_id, message_id, user_id, username, first_name,
             direction, timestamp, text, reply_to_id, reply_to_text,
             edited, deleted, raw_update_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE((SELECT edited FROM messages WHERE chat_id=? AND message_id=?), 0),
                COALESCE((SELECT deleted FROM messages WHERE chat_id=? AND message_id=?), 0),
                ?)
        """,
        (
            msg.chat_id,
            msg.message_id,
            msg.user_id,
            msg.username,
            msg.first_name,
            msg.direction,
            _iso(msg.timestamp),
            msg.text,
            msg.reply_to_id,
            msg.reply_to_text,
            msg.chat_id,
            msg.message_id,
            msg.chat_id,
            msg.message_id,
            msg.raw_update_json,
        ),
    )


async def mark_edited(db: Database, chat_id: int, message_id: int, new_text: str) -> None:
    await db.execute(
        "UPDATE messages SET text=?, edited=1 WHERE chat_id=? AND message_id=?",
        (new_text, chat_id, message_id),
    )


async def mark_deleted(db: Database, chat_id: int, message_id: int) -> None:
    await db.execute(
        "UPDATE messages SET deleted=1 WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    )


async def upsert_user(
    db: Database,
    chat_id: int,
    user_id: int,
    username: str | None,
    first_name: str | None,
    timestamp: datetime,
) -> None:
    iso = _iso(timestamp)
    existing = await db.fetch_one(
        "SELECT message_count FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    if existing is None:
        await db.execute(
            """
            INSERT INTO users(chat_id, user_id, username, first_name,
                              join_date, last_message_date, message_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (chat_id, user_id, username, first_name, iso, iso),
        )
    else:
        await db.execute(
            """
            UPDATE users
            SET username=?, first_name=?, last_message_date=?, message_count=message_count+1
            WHERE chat_id=? AND user_id=?
            """,
            (username, first_name, iso, chat_id, user_id),
        )


async def insert_tool_call(
    db: Database,
    *,
    tool_name: str,
    args_json: str,
    result_json: str | None,
    error: str | None,
    duration_ms: int,
) -> None:
    await db.execute(
        """
        INSERT INTO tool_calls(tool_name, args_json, result_json, error, duration_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (tool_name, args_json, result_json, error, duration_ms, _iso(datetime.now(timezone.utc))),
    )


async def fetch_reply_chain(
    db: Database,
    chat_id: int,
    reply_to_id: int,
    *,
    max_depth: int = 3,
) -> list[dict]:
    """Walk a Telegram reply chain in our own ``messages`` table.

    Returns a list of parent messages **oldest-first**, capped at
    ``max_depth`` hops. Each entry is a dict with ``message_id``, ``user_id``,
    ``username``, ``first_name``, ``direction``, ``timestamp``, and ``text``.

    The walk stops as soon as we hit a row whose ``reply_to_id`` is NULL or
    a row we don't have in the database. The lookup is keyed on
    ``(chat_id, message_id)`` because Telegram message ids are only unique
    inside a chat — the same id can appear in multiple chats.
    """
    chain: list[dict] = []
    cursor_id: int | None = reply_to_id
    seen: set[int] = set()
    for _ in range(max_depth):
        if cursor_id is None or cursor_id in seen:
            break
        seen.add(cursor_id)
        row = await db.fetch_one(
            """
            SELECT message_id, user_id, username, first_name,
                   direction, timestamp, text, reply_to_id
            FROM messages
            WHERE chat_id = ? AND message_id = ?
            """,
            (chat_id, cursor_id),
        )
        if row is None:
            break
        chain.append(
            {
                "message_id": row["message_id"],
                "user_id": row["user_id"],
                "username": row["username"],
                "first_name": row["first_name"],
                "direction": row["direction"],
                "timestamp": row["timestamp"],
                "text": row["text"],
            }
        )
        cursor_id = row["reply_to_id"]
    chain.reverse()  # oldest-first
    return chain


async def _load_reactions(
    db: Database, chat_id: int, message_id: int
) -> dict[str, list[int]]:
    row = await db.fetch_one(
        "SELECT reactions FROM messages WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    )
    if row is None or row["reactions"] is None:
        return {}
    try:
        data = json.loads(row["reactions"])
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: list(v) for k, v in data.items() if isinstance(v, list)}


async def _store_reactions(
    db: Database, chat_id: int, message_id: int, reactions: dict[str, list[int]]
) -> None:
    cleaned = {k: v for k, v in reactions.items() if v}
    payload = json.dumps(cleaned, ensure_ascii=False) if cleaned else None
    await db.execute(
        "UPDATE messages SET reactions=? WHERE chat_id=? AND message_id=?",
        (payload, chat_id, message_id),
    )


async def apply_user_reaction(
    db: Database,
    *,
    chat_id: int,
    message_id: int,
    user_id: int,
    old_emoji: Iterable[str],
    new_emoji: Iterable[str],
) -> None:
    """Reflect a Telegram ``MessageReactionUpdated`` event in the messages row.

    Removes ``user_id`` from every emoji in ``old_emoji`` and adds it to
    every emoji in ``new_emoji``. No-op if the message row doesn't exist.
    """
    reactions = await _load_reactions(db, chat_id, message_id)
    for emoji in old_emoji:
        users = reactions.get(emoji)
        if users and user_id in users:
            users.remove(user_id)
            if not users:
                reactions.pop(emoji, None)
    for emoji in new_emoji:
        users = reactions.setdefault(emoji, [])
        if user_id not in users:
            users.append(user_id)
    await _store_reactions(db, chat_id, message_id, reactions)


async def add_bot_reaction(
    db: Database,
    *,
    chat_id: int,
    message_id: int,
    bot_user_id: int,
    emoji: str,
) -> None:
    """Record a bot-sent reaction on the target message's row.

    Bots can only have one active reaction per message, so this replaces any
    prior bot reaction on the message (identified by ``bot_user_id``).
    """
    reactions = await _load_reactions(db, chat_id, message_id)
    for users in reactions.values():
        if bot_user_id in users:
            users.remove(bot_user_id)
    reactions = {k: v for k, v in reactions.items() if v}
    reactions.setdefault(emoji, []).append(bot_user_id)
    await _store_reactions(db, chat_id, message_id, reactions)
