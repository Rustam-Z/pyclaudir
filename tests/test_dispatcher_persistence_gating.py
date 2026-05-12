"""Issue #3 — disallowed chats must not reach SQLite or the engine.

Before this fix, ``_on_message`` persisted every inbound update *then*
checked the access policy, so strangers' messages landed in the
``messages`` / ``users`` tables. Same gap existed for edits and
reactions. These regression tests pin the corrected order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyclaudir.access import AccessConfig, save_access
from pyclaudir.config import Config
from pyclaudir.db.database import Database
from pyclaudir.db.messages import insert_message
from pyclaudir.models import ChatMessage
from pyclaudir.telegram_io import TelegramDispatcher


OWNER = 42
STRANGER = 100
DM_CHAT_STRANGER = 100
GROUP_CHAT = -1001234567890


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


def _cfg(tmp_path: Path, *, policy: str = "owner_only") -> Config:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    object.__setattr__(cfg, "owner_id", OWNER)
    save_access(
        cfg.access_path,
        AccessConfig(policy=policy, allowed_users=[], allowed_chats=[]),
    )
    return cfg


def _make_update(
    *,
    user_id: int,
    chat_id: int,
    chat_type: str,
    message_id: int = 1,
    text: str = "hello",
) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.username = "alice"
    user.first_name = "Alice"

    chat = MagicMock()
    chat.id = chat_id
    chat.type = chat_type
    chat.title = "T" if chat_type != "private" else None
    chat.full_name = "Alice"
    chat.username = "alice"

    msg = MagicMock()
    msg.chat_id = chat_id
    msg.chat = chat
    msg.message_id = message_id
    msg.from_user = user
    msg.text = text
    msg.caption = None
    msg.date = datetime.now(timezone.utc)
    msg.reply_to_message = None
    msg.photo = None
    msg.document = None

    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = chat
    update.effective_user = user
    update.edited_message = None
    update.message_reaction = None
    update.to_dict.return_value = {"u": 1}
    return update


def _edit_update(
    *, user_id: int, chat_id: int, chat_type: str, message_id: int
) -> MagicMock:
    upd = _make_update(
        user_id=user_id, chat_id=chat_id, chat_type=chat_type, message_id=message_id
    )
    upd.edited_message = upd.effective_message
    upd.edited_message.text = "edited text"
    return upd


def _reaction_update(
    *, user_id: int, chat_id: int, chat_type: str, message_id: int
) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    chat = MagicMock()
    chat.id = chat_id
    chat.type = chat_type
    chat.title = "T"
    chat.full_name = "Alice"
    chat.username = "alice"

    evt = MagicMock()
    evt.user = user
    evt.chat = chat
    evt.message_id = message_id
    evt.old_reaction = []
    new = MagicMock()
    new.emoji = "👍"
    evt.new_reaction = [new]

    upd = MagicMock()
    upd.message_reaction = evt
    upd.effective_chat = chat
    upd.effective_user = user
    upd.effective_message = None
    upd.edited_message = None
    return upd


def _dispatcher(cfg: Config, db: Database) -> TelegramDispatcher:
    dispatcher = TelegramDispatcher(cfg, db, engine=None, chat_titles={})
    dispatcher.engine = MagicMock()
    dispatcher.engine.submit = AsyncMock()
    dispatcher.engine.prime_typing = MagicMock()
    dispatcher.application = MagicMock()
    dispatcher.application.bot = AsyncMock()
    return dispatcher


async def _message_count(db: Database) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS c FROM messages")
    return int(row["c"])


async def _user_count(db: Database, user_id: int) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM users WHERE user_id=?", (user_id,)
    )
    return int(row["c"])


@pytest.mark.asyncio
async def test_disallowed_dm_not_persisted(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        dispatcher = _dispatcher(_cfg(tmp_path), db)
        await dispatcher._on_message(
            _make_update(
                user_id=STRANGER, chat_id=DM_CHAT_STRANGER, chat_type="private"
            ),
            None,
        )
        assert await _message_count(db) == 0
        assert await _user_count(db, STRANGER) == 0
        dispatcher.engine.submit.assert_not_awaited()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_disallowed_group_message_not_persisted(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        dispatcher = _dispatcher(_cfg(tmp_path), db)
        await dispatcher._on_message(
            _make_update(user_id=STRANGER, chat_id=GROUP_CHAT, chat_type="supergroup"),
            None,
        )
        assert await _message_count(db) == 0
        dispatcher.engine.submit.assert_not_awaited()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_dm_still_persisted(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        dispatcher = _dispatcher(_cfg(tmp_path), db)
        await dispatcher._on_message(
            _make_update(user_id=OWNER, chat_id=OWNER, chat_type="private"),
            None,
        )
        assert await _message_count(db) == 1
        assert await _user_count(db, OWNER) == 1
        dispatcher.engine.submit.assert_awaited_once()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_disallowed_edit_does_not_call_mark_edited(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        dispatcher = _dispatcher(_cfg(tmp_path), db)
        with patch(
            "pyclaudir.telegram_io.dispatcher.mark_edited", new=AsyncMock()
        ) as patched:
            await dispatcher._on_edited(
                _edit_update(
                    user_id=STRANGER,
                    chat_id=DM_CHAT_STRANGER,
                    chat_type="private",
                    message_id=1,
                ),
                None,
            )
            patched.assert_not_awaited()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_disallowed_reaction_does_not_mutate_db(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        # Seed a row so we can prove apply_user_reaction wasn't invoked
        # on it — if the gate failed, the reactions column would mutate.
        await insert_message(
            db,
            ChatMessage(
                chat_id=DM_CHAT_STRANGER,
                message_id=7,
                user_id=STRANGER,
                username="alice",
                first_name="Alice",
                direction="out",
                timestamp=datetime.now(timezone.utc),
                text="seed",
            ),
        )
        dispatcher = _dispatcher(_cfg(tmp_path), db)
        with patch(
            "pyclaudir.telegram_io.dispatcher.apply_user_reaction",
            new=AsyncMock(),
        ) as patched:
            await dispatcher._on_reaction(
                _reaction_update(
                    user_id=STRANGER,
                    chat_id=DM_CHAT_STRANGER,
                    chat_type="private",
                    message_id=7,
                ),
                None,
            )
            patched.assert_not_awaited()
        row = await db.fetch_one(
            "SELECT reactions FROM messages WHERE chat_id=? AND message_id=?",
            (DM_CHAT_STRANGER, 7),
        )
        assert row is not None
        assert row["reactions"] is None
    finally:
        await db.close()
