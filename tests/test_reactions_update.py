"""Verify reactions fold into the messages row correctly."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pyclaudir.config import Config
from pyclaudir.db.database import Database
from pyclaudir.db.messages import (
    add_bot_reaction,
    apply_user_reaction,
    insert_message,
)
from pyclaudir.models import ChatMessage


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


def _msg(chat_id: int = 1, message_id: int = 1) -> ChatMessage:
    return ChatMessage(
        chat_id=chat_id,
        message_id=message_id,
        user_id=100,
        username="alice",
        first_name="Alice",
        direction="out",
        timestamp=datetime.now(timezone.utc),
        text="hi",
    )


async def _reactions(db: Database, chat_id: int, message_id: int) -> dict:
    row = await db.fetch_one(
        "SELECT reactions FROM messages WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    )
    assert row is not None
    raw = row["reactions"]
    return json.loads(raw) if raw else {}


@pytest.mark.asyncio
async def test_user_adds_reaction(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await insert_message(db, _msg())
        await apply_user_reaction(
            db,
            chat_id=1,
            message_id=1,
            user_id=77,
            old_emoji=[],
            new_emoji=["👍"],
        )
        assert await _reactions(db, 1, 1) == {"👍": [77]}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_user_changes_reaction(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await insert_message(db, _msg())
        await apply_user_reaction(
            db, chat_id=1, message_id=1, user_id=77,
            old_emoji=[], new_emoji=["👍"],
        )
        await apply_user_reaction(
            db, chat_id=1, message_id=1, user_id=77,
            old_emoji=["👍"], new_emoji=["❤️"],
        )
        assert await _reactions(db, 1, 1) == {"❤️": [77]}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_user_removes_reaction(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await insert_message(db, _msg())
        await apply_user_reaction(
            db, chat_id=1, message_id=1, user_id=77,
            old_emoji=[], new_emoji=["👍"],
        )
        await apply_user_reaction(
            db, chat_id=1, message_id=1, user_id=77,
            old_emoji=["👍"], new_emoji=[],
        )
        row = await db.fetch_one(
            "SELECT reactions FROM messages WHERE chat_id=1 AND message_id=1"
        )
        assert row["reactions"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_multiple_users_same_emoji(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await insert_message(db, _msg())
        await apply_user_reaction(
            db, chat_id=1, message_id=1, user_id=77,
            old_emoji=[], new_emoji=["👍"],
        )
        await apply_user_reaction(
            db, chat_id=1, message_id=1, user_id=88,
            old_emoji=[], new_emoji=["👍"],
        )
        data = await _reactions(db, 1, 1)
        assert sorted(data["👍"]) == [77, 88]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_add_bot_reaction_replaces_prior_bot_reaction(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await insert_message(db, _msg())
        await add_bot_reaction(
            db, chat_id=1, message_id=1, bot_user_id=9999, emoji="👀",
        )
        await add_bot_reaction(
            db, chat_id=1, message_id=1, bot_user_id=9999, emoji="👍",
        )
        # Only the latest bot reaction should remain (Telegram bots can have
        # only one reaction per message).
        data = await _reactions(db, 1, 1)
        assert data == {"👍": [9999]}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reaction_update_on_missing_message_is_noop(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        # No INSERT into messages: row doesn't exist.
        await apply_user_reaction(
            db, chat_id=1, message_id=999, user_id=77,
            old_emoji=[], new_emoji=["👍"],
        )
        row = await db.fetch_one(
            "SELECT reactions FROM messages WHERE chat_id=1 AND message_id=999"
        )
        assert row is None
    finally:
        await db.close()
