"""Dispatcher-level rate limiter tests: DM-only, owner-exempt.

Also acts as a construction smoke test — catches the kind of
`_wire_handlers` regression we hit in the prior round (an invalid
handler type makes `TelegramDispatcher.__init__` crash).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyclaudir.access import AccessConfig, save_access
from pyclaudir.config import Config
from pyclaudir.db.database import Database
from pyclaudir.rate_limiter import RateLimiter
from pyclaudir.telegram_io import TelegramDispatcher


OWNER = 42
USER = 100
DM_CHAT = 100  # DMs: chat_id == user_id in Telegram
GROUP_CHAT = -1001234567890


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    # Open DMs so the gate lets non-owners through.
    save_access(
        cfg.access_path,
        AccessConfig(policy="open", allowed_users=[], allowed_chats=[GROUP_CHAT]),
    )
    return cfg


def _make_update(*, user_id: int, chat_id: int, chat_type: str, message_id: int = 1) -> MagicMock:
    """A MagicMock stand-in for a telegram Update, wired to the minimum
    surface the dispatcher reads."""
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
    msg.message_id = message_id
    msg.from_user = user
    msg.text = "hello"
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
    update.to_dict.return_value = {"u": 1}
    return update


def _dispatcher(cfg: Config, db: Database, rate_limiter: RateLimiter) -> TelegramDispatcher:
    return TelegramDispatcher(
        cfg,
        db,
        engine=None,
        chat_titles={},
        rate_limiter=rate_limiter,
    )


@pytest.mark.asyncio
async def test_dispatcher_constructs_with_rate_limiter(tmp_path: Path) -> None:
    """Smoke test: the dispatcher boots with a rate_limiter attached. Catches
    the `_wire_handlers` regression class directly."""
    db = await _open(tmp_path)
    try:
        # Give Config a real (but fake-valued) bot token for Application.builder().
        cfg = _cfg(tmp_path)
        limiter = RateLimiter(db=db, limit=3, owner_id=OWNER)
        dispatcher = _dispatcher(cfg, db, limiter)
        assert dispatcher.rate_limiter is limiter
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dm_spammer_is_rate_limited(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        cfg = _cfg(tmp_path)
        limiter = RateLimiter(db=db, limit=2, owner_id=OWNER)
        dispatcher = _dispatcher(cfg, db, limiter)
        dispatcher.engine = MagicMock()
        dispatcher.engine.submit = AsyncMock()
        # Replace the bot with an AsyncMock so the notice path doesn't try the network.
        dispatcher.application = MagicMock()
        dispatcher.application.bot = AsyncMock()

        # First 2 DMs from a non-owner go through.
        for mid in (1, 2):
            await dispatcher._on_message(
                _make_update(user_id=USER, chat_id=DM_CHAT, chat_type="private", message_id=mid),
                None,
            )
        assert dispatcher.engine.submit.await_count == 2

        # 3rd exceeds the limit — engine is NOT called, notice IS sent.
        await dispatcher._on_message(
            _make_update(user_id=USER, chat_id=DM_CHAT, chat_type="private", message_id=3),
            None,
        )
        assert dispatcher.engine.submit.await_count == 2
        dispatcher.application.bot.send_message.assert_awaited_once()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_group_spammer_is_not_rate_limited(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        cfg = _cfg(tmp_path)
        limiter = RateLimiter(db=db, limit=1, owner_id=OWNER)
        dispatcher = _dispatcher(cfg, db, limiter)
        dispatcher.engine = MagicMock()
        dispatcher.engine.submit = AsyncMock()
        dispatcher.application = MagicMock()
        dispatcher.application.bot = AsyncMock()

        # 5 messages in the same group from the same user — all forwarded.
        for mid in range(1, 6):
            await dispatcher._on_message(
                _make_update(
                    user_id=USER, chat_id=GROUP_CHAT, chat_type="supergroup", message_id=mid,
                ),
                None,
            )
        assert dispatcher.engine.submit.await_count == 5
        dispatcher.application.bot.send_message.assert_not_awaited()
        # No rate_limits rows were created for group chatter.
        row = await db.fetch_one("SELECT COUNT(*) AS c FROM rate_limits")
        assert row["c"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_bypasses_rate_limiter_in_dm(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        cfg = _cfg(tmp_path)
        limiter = RateLimiter(db=db, limit=1, owner_id=OWNER)
        dispatcher = _dispatcher(cfg, db, limiter)
        dispatcher.engine = MagicMock()
        dispatcher.engine.submit = AsyncMock()
        dispatcher.application = MagicMock()
        dispatcher.application.bot = AsyncMock()

        # Owner sends 10 DMs — all go through, none counted.
        for mid in range(1, 11):
            await dispatcher._on_message(
                _make_update(
                    user_id=OWNER, chat_id=OWNER, chat_type="private", message_id=mid,
                ),
                None,
            )
        assert dispatcher.engine.submit.await_count == 10
        dispatcher.application.bot.send_message.assert_not_awaited()
        row = await db.fetch_one(
            "SELECT COUNT(*) AS c FROM rate_limits WHERE user_id=?", (OWNER,)
        )
        assert row["c"] == 0
    finally:
        await db.close()
