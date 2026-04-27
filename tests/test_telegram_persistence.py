"""Persistence behaviour for Telegram messages, edits, deletes, and users."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pyclaudir.config import Config
from pyclaudir.db.database import Database
from pyclaudir.db.messages import (
    insert_message,
    mark_deleted,
    mark_edited,
    upsert_user,
)
from pyclaudir.models import ChatMessage


def _msg(text: str = "hello", message_id: int = 1, chat_id: int = -100, direction: str = "in") -> ChatMessage:
    return ChatMessage(
        chat_id=chat_id,
        message_id=message_id,
        user_id=42,
        username="alice",
        first_name="Alice",
        direction=direction,  # type: ignore[arg-type]
        timestamp=datetime(2026, 4, 11, 10, 31, tzinfo=timezone.utc),
        text=text,
    )


@pytest.fixture()
async def db(tmp_path: Path):
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_insert_message_round_trip(db: Database) -> None:
    await insert_message(db, _msg("hi"))
    rows = await db.fetch_all("SELECT direction, text FROM messages")
    assert len(rows) == 1
    assert rows[0]["direction"] == "in"
    assert rows[0]["text"] == "hi"


@pytest.mark.asyncio
async def test_insert_message_idempotent_on_replay(db: Database) -> None:
    await insert_message(db, _msg("v1"))
    await insert_message(db, _msg("v1"))  # same id replays
    rows = await db.fetch_all("SELECT * FROM messages")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_mark_edited_sets_flag(db: Database) -> None:
    await insert_message(db, _msg("v1"))
    await mark_edited(db, chat_id=-100, message_id=1, new_text="v2")
    row = await db.fetch_one("SELECT text, edited FROM messages WHERE message_id=1")
    assert row["text"] == "v2"
    assert row["edited"] == 1


@pytest.mark.asyncio
async def test_mark_deleted_sets_flag(db: Database) -> None:
    await insert_message(db, _msg("hi"))
    await mark_deleted(db, chat_id=-100, message_id=1)
    row = await db.fetch_one("SELECT deleted FROM messages WHERE message_id=1")
    assert row["deleted"] == 1


@pytest.mark.asyncio
async def test_inbound_and_outbound_coexist(db: Database) -> None:
    await insert_message(db, _msg("from user", message_id=1, direction="in"))
    await insert_message(db, _msg("from bot", message_id=2, direction="out"))
    rows = await db.fetch_all("SELECT direction FROM messages ORDER BY message_id")
    dirs = [r["direction"] for r in rows]
    assert dirs == ["in", "out"]


@pytest.mark.asyncio
async def test_upsert_user_increments_count(db: Database) -> None:
    ts = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)
    await upsert_user(db, chat_id=-100, user_id=42, username="a", first_name="A", timestamp=ts)
    await upsert_user(db, chat_id=-100, user_id=42, username="a", first_name="A", timestamp=ts)
    row = await db.fetch_one("SELECT message_count FROM users")
    assert row["message_count"] == 2


@pytest.mark.asyncio
async def test_dispatcher_drops_disallowed_chats() -> None:
    """The gate() function blocks disallowed chats. Owner DMs always pass;
    stranger DMs and unlisted groups are dropped."""
    from pyclaudir.access import AccessConfig, gate

    access = AccessConfig(policy="owner_only", allowed_users=[], allowed_chats=[])
    assert gate(access=access, owner_id=42, chat_id=-100999, user_id=999, chat_type="supergroup") is False
    assert gate(access=access, owner_id=42, chat_id=42, user_id=42, chat_type="private") is True
