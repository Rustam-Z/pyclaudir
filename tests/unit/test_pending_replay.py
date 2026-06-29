"""Pending-buffer persistence — consumed flag + boot replay (given/when/then).

A process crash between buffering and the turn send must not silently
drop messages: the dispatcher already persisted them, the engine marks
them ``consumed`` once handed to CC, and startup replays the leftovers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hamroh.cc_worker import TurnResult
from hamroh.config import Config
from hamroh.db.database import Database
from hamroh.db.messages import (
    fetch_unconsumed_inbound,
    insert_message,
    mark_messages_consumed,
)
from hamroh.engine import Engine, EngineOptions
from hamroh.models import ChatMessage
from hamroh.startup import _replay_unconsumed

_CFG = Config.for_test(Path("/tmp"))


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


def _msg(
    text: str, mid: int, *, direction: str = "in", chat_id: int = -100
) -> ChatMessage:
    return ChatMessage(
        chat_id=chat_id,
        message_id=mid,
        user_id=42,
        username="alice",
        first_name="Alice",
        direction=direction,  # type: ignore[arg-type]
        timestamp=datetime.now(timezone.utc),
        text=text,
    )


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.injected: list[str] = []
        self._results: asyncio.Queue[TurnResult] = asyncio.Queue()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def inject(self, text: str) -> None:
        self.injected.append(text)

    async def wait_for_result(self) -> TurnResult:
        return await self._results.get()


@pytest.mark.asyncio
async def test_unconsumed_roundtrip(tmp_path: Path) -> None:
    # Given one inbound and one outbound row
    db = await _open(tmp_path)
    try:
        await insert_message(db, _msg("hello", 1))
        await insert_message(db, _msg("reply", 2, direction="out"))

        # When fetching unconsumed inbound
        pending = await fetch_unconsumed_inbound(db)
        assert [m.message_id for m in pending] == [1], "only inbound rows replay"
        assert pending[0].text == "hello", "message body survives the round trip"

        # When the batch is marked consumed
        await mark_messages_consumed(db, [(-100, 1)])
        assert await fetch_unconsumed_inbound(db) == [], "consumed rows never replay"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_turn_marks_batch_consumed(tmp_path: Path) -> None:
    """A drained turn flags its batch in the DB — the crash-replay set
    is exactly 'buffered but never handed to CC'."""
    db = await _open(tmp_path)
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20, db=db))
    await eng.start()
    try:
        msg = _msg("persisted then buffered", 7)
        await insert_message(db, msg)  # dispatcher does this before submit
        await eng.submit(msg)
        await asyncio.sleep(0.1)  # debounce fires, turn kicks
        assert worker.sent, "turn must have started"

        row = await db.fetch_one(
            "SELECT consumed FROM messages WHERE chat_id=-100 AND message_id=7"
        )
        assert row["consumed"] == 1, "drained batch must be flagged consumed"
    finally:
        await eng.stop()
        await db.close()


@pytest.mark.asyncio
async def test_boot_replay_resubmits_unconsumed(tmp_path: Path) -> None:
    """Crash simulation: a persisted-but-unconsumed message reaches CC
    after restart via the boot replay."""
    db = await _open(tmp_path)
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20, db=db))
    await eng.start()
    try:
        await insert_message(db, _msg("lost in the crash", 9))

        await _replay_unconsumed(db, eng)
        await asyncio.sleep(0.1)  # debounce fires

        joined = "\n".join(worker.sent)
        assert "lost in the crash" in joined, "replayed message must reach the worker"
        row = await db.fetch_one("SELECT consumed FROM messages WHERE message_id=9")
        assert row["consumed"] == 1, "replayed message is consumed by its turn"
    finally:
        await eng.stop()
        await db.close()


@pytest.mark.asyncio
async def test_stale_unconsumed_settled_not_replayed(tmp_path: Path) -> None:
    """Messages older than 24h don't get a surprise reply — they are
    settled instead."""
    db = await _open(tmp_path)
    try:
        await db.execute(
            "INSERT INTO messages (chat_id, message_id, user_id, direction, "
            "timestamp, text) VALUES (-100, 11, 42, 'in', "
            "datetime('now', '-2 days'), 'ancient question')"
        )
        pending = await fetch_unconsumed_inbound(db)
        assert pending == [], "stale rows must not replay"
        row = await db.fetch_one("SELECT consumed FROM messages WHERE message_id=11")
        assert row["consumed"] == 1, "stale rows are settled so they don't linger"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_synthetic_reminders_skipped(tmp_path: Path) -> None:
    """Reminder messages (message_id=0) re-fire via their own pending
    status — the consumed path must ignore them."""
    db = await _open(tmp_path)
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20, db=db))
    await eng.start()
    try:
        await eng.submit(_msg("<reminder>tick</reminder>", 0))
        await asyncio.sleep(0.1)
        assert worker.sent, "reminder turn must still start"
        # No row exists for message_id=0; the engine must not have tried
        # to mark it (a row-values UPDATE with no keys would be a bug).
        pending = await fetch_unconsumed_inbound(db)
        assert pending == []
    finally:
        await eng.stop()
        await db.close()


@pytest.mark.asyncio
async def test_migration_backfills_history(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        cols = await db.fetch_all("PRAGMA table_info(messages)")
        names = {c["name"] for c in cols}
        assert "consumed" in names, "migration 007 must add the consumed column"
        idx = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_messages_unconsumed'"
        )
        assert idx, "partial index for the boot query must exist"
    finally:
        await db.close()
