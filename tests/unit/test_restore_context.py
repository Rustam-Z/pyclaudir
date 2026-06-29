"""Restored-context digest — carry conversation into a fresh CC session.

When hamroh resets the CC session (API-error auto-reset, stale-session
recovery, owner /reset_session) the model loses its context. The engine
stashes a small sanitized digest built from our own SQLite history and
prepends it to the fresh session's first turn. The digest must be safe to
send: lone surrogates and control chars stripped (they break the CC
stdin pipe), per-message truncation, failed-batch exclusion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hamroh.cc_worker import TurnResult
from hamroh.config import Config
from hamroh.db.database import Database
from hamroh.db.messages import (
    RecentMessagesQuery,
    fetch_recent_messages,
    insert_message,
    mark_deleted,
    mark_edited,
    mark_messages_consumed,
    mark_messages_processed,
)
from hamroh.engine import Engine, EngineOptions
from hamroh.engine.restore import build_restored_context, sanitize_for_cc
from hamroh.models import ChatMessage
from hamroh.startup import _App, _make_on_cc_stale_session

CHAT = -1001234567890


def _msg(
    mid: int,
    text: str,
    *,
    direction: str = "in",
    user_id: int = 42,
) -> ChatMessage:
    return ChatMessage(
        chat_id=CHAT,
        message_id=mid,
        user_id=user_id,
        username="alice" if direction == "in" else "bot",
        first_name="Alice" if direction == "in" else "Bot",
        direction=direction,  # type: ignore[arg-type]
        timestamp=datetime(2026, 6, 12, 10, mid % 60, tzinfo=timezone.utc),
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


async def _insert_consumed(db: Database, msg: ChatMessage) -> None:
    """A message CC received but whose turn never finished cleanly."""
    await insert_message(db, msg)
    await mark_messages_consumed(db, [(msg.chat_id, msg.message_id)])


async def _insert_processed(db: Database, msg: ChatMessage) -> None:
    """A message whose turn completed cleanly — digest-eligible."""
    await _insert_consumed(db, msg)
    await mark_messages_processed(db, [(msg.chat_id, msg.message_id)])


# ----------------------------------------------------------------------
# sanitize_for_cc
# ----------------------------------------------------------------------


def test_sanitize_strips_lone_surrogates() -> None:
    # Given text with a lone surrogate — the exact thing that makes
    # CcWorker.send's line.encode("utf-8") raise
    dirty = "a\ud800b\udfffc"

    # When sanitized
    clean = sanitize_for_cc(dirty)

    # Then surrogates are gone and the result is safely encodable
    assert clean == "abc", "lone surrogates must be removed"
    clean.encode("utf-8")  # must not raise


def test_sanitize_strips_controls_keeps_newline_tab() -> None:
    dirty = "a\x00b\x1bc\x7fd\re\nf\tg"
    clean = sanitize_for_cc(dirty)
    assert clean == "abcde\nf\tg", (
        "C0/C1/DEL/CR must be stripped; newline and tab must survive"
    )


def test_sanitize_truncates_to_cap_with_ellipsis() -> None:
    clean = sanitize_for_cc("x" * 600, cap=500)
    assert len(clean) == 501, "cap chars plus the ellipsis"
    assert clean.endswith("…"), "truncation must be visible to the model"


def test_sanitize_keeps_normal_unicode() -> None:
    text = "héllo 👋 مرحبا שלום"
    assert sanitize_for_cc(text) == text, (
        "emoji and RTL letters are legitimate chat text"
    )


# ----------------------------------------------------------------------
# fetch_recent_messages
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_recent_last_n_oldest_first_both_directions(db: Database) -> None:
    # Given 12 trusted inbound messages plus a bot reply
    for mid in range(1, 13):
        await _insert_processed(db, _msg(mid, f"user {mid}"))
    await insert_message(db, _msg(100, "bot reply", direction="out", user_id=1))

    # When fetching the last 10
    rows = await fetch_recent_messages(db, RecentMessagesQuery(limit=10))

    # Then the newest 10 come back oldest-first, both directions included
    assert len(rows) == 10, "limit must cap the digest size"
    assert rows[0]["text"] == "user 4", "oldest of the kept window comes first"
    assert rows[-1]["text"] == "bot reply", "outbound replies belong in the digest"
    assert {r["direction"] for r in rows} == {"in", "out"}


@pytest.mark.asyncio
async def test_fetch_recent_skips_deleted_and_untrusted_inbound(db: Database) -> None:
    # Given a trusted message, a deleted one, an unconsumed pending one
    # (will replay as a live <msg>), a consumed-but-unprocessed one
    # (its turn FAILED — never committed), and an outbound row
    # (outbound rows keep consumed=0/processed=0 by design)
    await _insert_processed(db, _msg(1, "kept"))
    await _insert_processed(db, _msg(2, "deleted later"))
    await mark_deleted(db, CHAT, 2)
    await insert_message(db, _msg(3, "still pending"))
    await _insert_consumed(db, _msg(4, "poison from a failed turn"))
    await insert_message(db, _msg(5, "bot reply", direction="out", user_id=1))

    rows = await fetch_recent_messages(db, RecentMessagesQuery(limit=10))

    texts = [r["text"] for r in rows]
    assert texts == ["kept", "bot reply"], (
        "deleted, pending, and failed-turn inbound rows must not be "
        "digested; outbound rows must survive their default processed=0"
    )


@pytest.mark.asyncio
async def test_fetch_recent_untrusted_dont_count_against_limit(db: Database) -> None:
    # Given 12 trusted messages and 2 newer ones from a failed turn
    for mid in range(1, 13):
        await _insert_processed(db, _msg(mid, f"m{mid}"))
    await _insert_consumed(db, _msg(13, "failed turn a"))
    await _insert_consumed(db, _msg(14, "failed turn b"))

    rows = await fetch_recent_messages(db, RecentMessagesQuery(limit=10))

    # Then the limit is filled entirely from trusted history
    assert [r["message_id"] for r in rows] == list(range(3, 13)), (
        "untrusted rows must not shrink the digest below the limit"
    )


@pytest.mark.asyncio
async def test_mark_edited_resets_processed(db: Database) -> None:
    # Given a trusted (committed) message
    await _insert_processed(db, _msg(1, "benign"))

    # When the user edits it
    await mark_edited(db, CHAT, 1, "edited into poison")

    # Then it loses trust and leaves digest range
    rows = await fetch_recent_messages(db, RecentMessagesQuery(limit=10))
    assert rows == [], (
        "edited content must re-earn processed=1 — a committed message "
        "can't be edited into poison after the fact"
    )


# ----------------------------------------------------------------------
# build_restored_context
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_returns_none_without_db_or_rows(db: Database) -> None:
    assert await build_restored_context(None, reason="api-error") is None, (
        "no database → plain fresh turn"
    )
    assert await build_restored_context(db, reason="api-error") is None, (
        "no history → plain fresh turn"
    )


@pytest.mark.asyncio
async def test_build_renders_reason_note_and_escaped_bodies(db: Database) -> None:
    # Given trusted history containing XML-special characters
    await _insert_processed(db, _msg(1, "see <b>this</b> & that"))

    # When the digest is built
    block = await build_restored_context(db, reason="api-error")

    # Then it is a well-formed, escaped, annotated block
    assert block is not None
    assert block.startswith('<restored_context reason="api-error">')
    assert block.endswith("</restored_context>")
    assert "database_query" in block, "the note must point at older history"
    assert "&lt;b&gt;this&lt;/b&gt; &amp; that" in block, (
        "bodies must be XML-escaped, never raw"
    )
    assert "<history_msg" in block and "<msg" not in block.replace(
        "<history_msg", ""
    ).replace("</history_msg", ""), (
        "digest entries must not look like live <msg> blocks"
    )


# ----------------------------------------------------------------------
# Engine: stash consumption in _kick, consumed-key tracking
# ----------------------------------------------------------------------


def _engine(tmp_path: Path, db: Database) -> tuple[Engine, MagicMock]:
    worker = MagicMock(send=AsyncMock(), inject=AsyncMock(), reset_session=AsyncMock())
    engine = Engine(worker, Config.for_test(tmp_path), EngineOptions(db=db))
    return engine, worker


@pytest.mark.asyncio
async def test_kick_prepends_stash_once(tmp_path: Path, db: Database) -> None:
    # Given a stashed digest and a pending message
    engine, worker = _engine(tmp_path, db)
    engine._restore_context = (
        '<restored_context reason="api-error">old</restored_context>'
    )
    engine._pending = [_msg(1, "hello")]

    # When the turn kicks
    await engine._kick()

    # Then the digest leads the payload, exactly once
    payload = worker.send.await_args.args[0]
    assert payload.startswith("<restored_context"), "digest must open the turn"
    assert '<msg id="1"' in payload, "the live batch must follow the digest"
    assert engine._restore_context is None, "stash is one-shot"
    assert engine._turn.had_restored_context is True, "poison guard must arm"

    # And the next turn is plain again
    engine._is_processing.clear()
    engine._pending = [_msg(2, "again")]
    await engine._kick()
    payload2 = worker.send.await_args.args[0]
    assert "<restored_context" not in payload2, "digest must not repeat"
    assert engine._turn.had_restored_context is False, "guard must disarm"


@pytest.mark.asyncio
async def test_mark_consumed_accumulates_turn_keys(
    tmp_path: Path, db: Database
) -> None:
    # Given a turn started from one message
    engine, _worker = _engine(tmp_path, db)
    engine._pending = [_msg(1, "first")]
    await engine._kick()
    assert engine._turn.consumed_keys == [(CHAT, 1)]

    # When another message injects mid-turn
    engine._pending = [_msg(2, "follow-up")]
    await engine._maybe_inject()

    # Then both batches belong to this turn's keys
    assert engine._turn.consumed_keys == [(CHAT, 1), (CHAT, 2)], (
        "the clean-turn commit needs every message CC saw this turn"
    )

    # And a new turn starts with a clean slate
    engine._is_processing.clear()
    engine._pending = [_msg(3, "next turn")]
    await engine._kick()
    assert engine._turn.consumed_keys == [(CHAT, 3)], "keys are per-turn state"


async def _processed_flag(db: Database, mid: int) -> int:
    row = await db.fetch_one(
        "SELECT processed FROM messages WHERE chat_id=? AND message_id=?",
        (CHAT, mid),
    )
    assert row is not None, f"message {mid} must exist"
    return row["processed"]


@pytest.mark.asyncio
async def test_clean_turn_commits_its_messages(tmp_path: Path, db: Database) -> None:
    # Given a turn built from one kicked and one injected message
    engine, _worker = _engine(tmp_path, db)
    await insert_message(db, _msg(1, "first"))
    await insert_message(db, _msg(2, "follow-up"))
    engine._pending = [_msg(1, "first")]
    await engine._kick()
    engine._pending = [_msg(2, "follow-up")]
    await engine._maybe_inject()

    # When the turn completes cleanly (no reply sent — still a success)
    await engine._handle_turn_result(TurnResult())

    # Then both messages are committed as trusted
    assert await _processed_flag(db, 1) == 1, "kicked message must commit"
    assert await _processed_flag(db, 2) == 1, "injected message must commit"


@pytest.mark.asyncio
async def test_failed_turn_leaves_messages_untrusted(
    tmp_path: Path, db: Database
) -> None:
    # Given a turn whose batch the API then rejects
    engine, worker = _engine(tmp_path, db)
    await insert_message(db, _msg(1, "the poisoned request"))
    engine._pending = [_msg(1, "the poisoned request")]
    await engine._kick()

    # When the turn fails with an unclassified API error
    await engine._handle_turn_result(
        TurnResult(api_error="API Error: violates our Usage Policy")
    )

    # Then the message stays untrusted and out of every future digest
    assert await _processed_flag(db, 1) == 0, (
        "only a clean turn may commit — failure must leave processed=0"
    )
    rows = await fetch_recent_messages(db, RecentMessagesQuery(limit=10))
    assert rows == [], "the failed batch must never re-enter a session"
    worker.reset_session.assert_awaited_once()


# ----------------------------------------------------------------------
# Stale-session callback wiring
# ----------------------------------------------------------------------


def _app(tmp_path: Path) -> _App:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    app = _App(config=cfg, db=MagicMock())
    app.dispatcher = MagicMock()
    app.dispatcher.bot.send_message = AsyncMock()
    return app


@pytest.mark.asyncio
async def test_stale_session_callback_stashes_digest(tmp_path: Path) -> None:
    # Given a wired engine
    app = _app(tmp_path)
    app.engine = MagicMock(stash_restore_context=AsyncMock())

    # When CC rejects the persisted session id
    await _make_on_cc_stale_session(app)("dead-session-id")

    # Then the next fresh turn will carry a recap
    app.engine.stash_restore_context.assert_awaited_once_with("stale-session")


@pytest.mark.asyncio
async def test_stale_session_callback_tolerates_missing_engine(
    tmp_path: Path,
) -> None:
    # Given a stale rejection arriving before the engine is built
    app = _app(tmp_path)
    assert app.engine is None

    # When the callback fires, it must not raise
    await _make_on_cc_stale_session(app)("dead-session-id")
