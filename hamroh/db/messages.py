"""Persistence helpers for the ``messages``, ``users``, and ``tool_calls`` tables.

Kept as plain functions taking a :class:`Database` so they're trivial to mock
in tests and don't entangle the database wrapper with PTB types.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .database import Database
from ..models import ChatMessage


@dataclass(frozen=True)
class MessageKey:
    """Identifies one message. Telegram ids are only unique within a chat,
    so both halves are always needed together."""

    chat_id: int
    message_id: int


@dataclass(frozen=True)
class ReactionChange:
    """One user's reaction transition from a ``MessageReactionUpdated`` event."""

    user_id: int
    old_emoji: Iterable[str]
    new_emoji: Iterable[str]


@dataclass(frozen=True)
class ToolCall:
    """One MCP tool invocation to persist in the ``tool_calls`` audit table."""

    tool_name: str
    args_json: str
    result_json: str | None
    error: str | None
    duration_ms: int


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _from_iso(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


async def mark_messages_consumed(db: Database, keys: list[tuple[int, int]]) -> None:
    """Flag ``(chat_id, message_id)`` rows as handed to the CC subprocess.

    Called by the engine once per turn, AFTER the send — never on the
    per-message hot path. A single row-values UPDATE statement.
    """
    if not keys:
        return
    placeholders = ",".join("(?,?)" for _ in keys)
    params = [v for pair in keys for v in pair]
    await db.execute(
        "UPDATE messages SET consumed=1 "
        f"WHERE (chat_id, message_id) IN (VALUES {placeholders})",
        params,
    )


async def mark_messages_processed(db: Database, keys: list[tuple[int, int]]) -> None:
    """Flag this turn's inbound rows as successfully processed by CC.

    The only thing that makes a message digest-eligible (see
    ``engine/restore.py``): rows from failed, aborted, or crashed turns
    never get this UPDATE and stay barred from restored context forever.
    Called by the engine once per CLEAN turn — never on the hot path.
    """
    if not keys:
        return
    placeholders = ",".join("(?,?)" for _ in keys)
    params = [v for pair in keys for v in pair]
    await db.execute(
        "UPDATE messages SET processed=1 "
        f"WHERE (chat_id, message_id) IN (VALUES {placeholders})",
        params,
    )


async def fetch_unconsumed_inbound(db: Database) -> list[ChatMessage]:
    """Inbound messages buffered but never handed to CC — replayed on boot.

    Only rows from the last 24 hours are returned; anything older is
    settled (``consumed=1``) instead, so days-old questions don't get a
    surprise reply and stale rows leave the partial index. The stored
    text is already scrubbed + normalized, so ``input_flags`` is empty.
    """
    cutoff = "datetime('now', '-24 hours')"
    rows = await db.fetch_all(
        "SELECT chat_id, message_id, user_id, username, first_name, "
        "timestamp, text, reply_to_id, reply_to_text FROM messages "
        "WHERE direction='in' AND consumed=0 AND message_id > 0 "
        f"AND timestamp > {cutoff} ORDER BY rowid",
    )
    await db.execute(
        "UPDATE messages SET consumed=1 "
        f"WHERE direction='in' AND consumed=0 AND timestamp <= {cutoff}",
    )
    return [
        ChatMessage(
            chat_id=r["chat_id"],
            message_id=r["message_id"],
            user_id=r["user_id"],
            username=r["username"],
            first_name=r["first_name"],
            direction="in",
            timestamp=_from_iso(r["timestamp"]),
            text=r["text"],
            reply_to_id=r["reply_to_id"],
            reply_to_text=r["reply_to_text"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class RecentMessagesQuery:
    """Filters for :func:`fetch_recent_messages`.

    ``before_message_id`` paginates *within a chat* (message ids are per-chat),
    so pass it together with ``chat_id``.
    """

    limit: int
    include_unprocessed: bool = False
    chat_id: int | None = None
    before_message_id: int | None = None


async def fetch_recent_messages(db: Database, query: RecentMessagesQuery) -> list[dict]:
    """Most recent real messages, both directions, oldest-first.

    Always skips deleted and synthetic (``message_id <= 0``) rows. Outbound
    rows are always eligible: Telegram confirmed their delivery.

    ``include_unprocessed=False`` (default) also skips inbound rows without
    ``processed=1`` — only a cleanly completed turn commits its messages, so a
    failed/aborted turn (or a still-pending live ``<msg>``) never re-enters a
    fresh session. The restored-context digest (``engine/restore.py``) needs
    this; the ``database_get_recent_messages`` tool passes ``True`` for live
    recall and narrows further with ``chat_id`` / ``before_message_id``.
    """
    where = ["message_id > 0", "deleted = 0"]
    params: list[int] = []
    if not query.include_unprocessed:
        where.append("NOT (direction = 'in' AND processed = 0)")
    if query.chat_id is not None:
        where.append("chat_id = ?")
        params.append(query.chat_id)
    if query.before_message_id is not None:
        where.append("message_id < ?")
        params.append(query.before_message_id)
    params.append(query.limit)
    rows = await db.fetch_all(
        "SELECT chat_id, message_id, user_id, username, first_name, "
        "direction, timestamp, text FROM messages WHERE "
        + " AND ".join(where)
        + " ORDER BY rowid DESC LIMIT ?",
        params,
    )
    kept = [dict(r) for r in rows]
    kept.reverse()  # oldest-first
    return kept


async def insert_message(db: Database, msg: ChatMessage) -> None:
    """Idempotently insert a Telegram message row.

    Edited messages re-fire the handler with the same ``message_id``; the
    upsert keeps the row current while leaving ``edited``/``deleted`` (not
    in the SET list) untouched. The ``edited`` flag is bumped via
    :func:`mark_edited` from the edited-message handler instead.
    """
    await db.execute(
        """
        INSERT INTO messages
            (chat_id, message_id, user_id, username, first_name,
             direction, timestamp, text, reply_to_id, reply_to_text,
             raw_update_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
            user_id=excluded.user_id,
            username=excluded.username,
            first_name=excluded.first_name,
            direction=excluded.direction,
            timestamp=excluded.timestamp,
            text=excluded.text,
            reply_to_id=excluded.reply_to_id,
            reply_to_text=excluded.reply_to_text,
            raw_update_json=excluded.raw_update_json
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
            msg.raw_update_json,
        ),
    )


async def mark_edited(
    db: Database, chat_id: int, message_id: int, new_text: str
) -> None:
    """Update an edited message's text and reset its trust flag.

    Edited content must re-earn ``processed=1`` — and since edits are
    not re-submitted to the engine, an edited row effectively leaves
    the restored-context digest for good (a committed benign message
    can't be edited into poison after the fact).
    """
    await db.execute(
        "UPDATE messages SET text=?, edited=1, processed=0 "
        "WHERE chat_id=? AND message_id=?",
        (new_text, chat_id, message_id),
    )


async def mark_deleted(db: Database, chat_id: int, message_id: int) -> None:
    await db.execute(
        "UPDATE messages SET deleted=1 WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    )


async def upsert_user(db: Database, msg: ChatMessage) -> None:
    """Create or refresh the sender's ``users`` row from one inbound message."""
    iso = _iso(msg.timestamp)
    existing = await db.fetch_one(
        "SELECT message_count FROM users WHERE chat_id=? AND user_id=?",
        (msg.chat_id, msg.user_id),
    )
    if existing is None:
        await db.execute(
            """
            INSERT INTO users(chat_id, user_id, username, first_name,
                              join_date, last_message_date, message_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (msg.chat_id, msg.user_id, msg.username, msg.first_name, iso, iso),
        )
    else:
        await db.execute(
            """
            UPDATE users
            SET username=?, first_name=?, last_message_date=?, message_count=message_count+1
            WHERE chat_id=? AND user_id=?
            """,
            (msg.username, msg.first_name, iso, msg.chat_id, msg.user_id),
        )


async def insert_tool_call(db: Database, call: ToolCall) -> None:
    await db.execute(
        """
        INSERT INTO tool_calls(tool_name, args_json, result_json, error, duration_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            call.tool_name,
            call.args_json,
            call.result_json,
            call.error,
            call.duration_ms,
            _iso(datetime.now(timezone.utc)),
        ),
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
        chain.append(_reply_chain_entry(row))
        cursor_id = row["reply_to_id"]
    chain.reverse()  # oldest-first
    return chain


def _reply_chain_entry(row: Any) -> dict:
    """Project one ``messages`` row into the reply-chain dict shape."""
    return {
        "message_id": row["message_id"],
        "user_id": row["user_id"],
        "username": row["username"],
        "first_name": row["first_name"],
        "direction": row["direction"],
        "timestamp": row["timestamp"],
        "text": row["text"],
    }


async def _load_reactions(db: Database, key: MessageKey) -> dict[str, list[int]]:
    row = await db.fetch_one(
        "SELECT reactions FROM messages WHERE chat_id=? AND message_id=?",
        (key.chat_id, key.message_id),
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
    db: Database, key: MessageKey, reactions: dict[str, list[int]]
) -> None:
    cleaned = {k: v for k, v in reactions.items() if v}
    payload = json.dumps(cleaned, ensure_ascii=False) if cleaned else None
    await db.execute(
        "UPDATE messages SET reactions=? WHERE chat_id=? AND message_id=?",
        (payload, key.chat_id, key.message_id),
    )


async def apply_user_reaction(
    db: Database, key: MessageKey, change: ReactionChange
) -> None:
    """Reflect a Telegram ``MessageReactionUpdated`` event in the messages row.

    Removes the user from every emoji in ``change.old_emoji`` and adds them to
    every emoji in ``change.new_emoji``. No-op if the message row doesn't exist.
    """
    reactions = await _load_reactions(db, key)
    for emoji in change.old_emoji:
        users = reactions.get(emoji)
        if users and change.user_id in users:
            users.remove(change.user_id)
            if not users:
                reactions.pop(emoji, None)
    for emoji in change.new_emoji:
        users = reactions.setdefault(emoji, [])
        if change.user_id not in users:
            users.append(change.user_id)
    await _store_reactions(db, key, reactions)


async def add_bot_reaction(
    db: Database, key: MessageKey, bot_user_id: int, emoji: str
) -> None:
    """Record a bot-sent reaction on the target message's row.

    Bots can only have one active reaction per message, so this replaces any
    prior bot reaction on the message (identified by ``bot_user_id``).
    """
    reactions = await _load_reactions(db, key)
    for users in reactions.values():
        if bot_user_id in users:
            users.remove(bot_user_id)
    reactions = {k: v for k, v in reactions.items() if v}
    reactions.setdefault(emoji, []).append(bot_user_id)
    await _store_reactions(db, key, reactions)
