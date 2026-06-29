"""API-rejected turns — notify fast, then auto-reset to a fresh session.

When the Anthropic API refuses a turn (e.g. a usage-policy violation from
an injected "ignore previous instructions" payload), the result event
carries ``is_error: true`` and the rejected content stays in the resumed
session history — every later turn replays it and fails too. The worker
must mark the turn as failed, and the engine must skip the dropped-text
retry loop, tell the user, and respawn CC with a fresh session.
Classified transient failures (rate-limit & co.) keep the session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hamroh.cc_worker import CcSpawnSpec, CcWorker, TurnResult
from hamroh.config import Config
from hamroh.db.database import Database
from hamroh.db.messages import (
    insert_message,
    mark_messages_consumed,
    mark_messages_processed,
)
from hamroh.engine import Engine, EngineOptions
from hamroh.engine.engine import TurnCallbacks
from hamroh.models import ChatMessage

POLICY_ERROR = (
    "API Error: Claude Code is unable to respond to this request, "
    "which appears to violate our Usage Policy. Try rephrasing the "
    "request in a new session."
)


# ----------------------------------------------------------------------
# Worker: result event marks the turn as failed
# ----------------------------------------------------------------------


def _worker(tmp_path: Path) -> CcWorker:
    sp = tmp_path / "system.md"
    sp.write_text("system")
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {}}')
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    spec = CcSpawnSpec(
        binary="/bin/true",  # never actually spawned
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
    )
    return CcWorker(spec, Config.for_test(tmp_path))


def test_error_result_event_sets_api_error(tmp_path: Path) -> None:
    # Given a worker mid-turn that produced the API's refusal text
    worker = _worker(tmp_path)
    worker._current_turn = TurnResult(text_blocks=[POLICY_ERROR])

    # When the error result event arrives (observed shape: subtype is
    # still "success" but is_error is set)
    worker._handle_event(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": POLICY_ERROR,
        }
    )

    # Then the queued TurnResult carries the error for the engine
    result = worker._result_queue.get_nowait()
    assert result.api_error == POLICY_ERROR, (
        "engine needs the API error text to classify and notify"
    )


def test_clean_result_event_leaves_api_error_none(tmp_path: Path) -> None:
    # Given a worker mid-turn
    worker = _worker(tmp_path)
    worker._current_turn = TurnResult(text_blocks=["hi"])

    # When a normal result event arrives
    worker._handle_event({"type": "result", "subtype": "success"})

    # Then the turn is not marked as failed
    result = worker._result_queue.get_nowait()
    assert result.api_error is None, "clean turns must not look like failures"


# ----------------------------------------------------------------------
# Engine: api_error branch
# ----------------------------------------------------------------------


def _engine(
    tmp_path: Path, db: Database | None = None
) -> tuple[Engine, MagicMock, list[tuple[int, str]]]:
    worker = MagicMock(reset_session=AsyncMock(), send=AsyncMock())
    sent: list[tuple[int, str]] = []

    async def notify(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    engine = Engine(
        worker,
        Config.for_test(tmp_path),
        EngineOptions(db=db, error_notify=notify),
    )
    engine._is_processing.set()
    engine._turn.active_chats = {-100}
    return engine, worker, sent


@pytest.mark.asyncio
async def test_policy_error_notifies_and_resets_session(tmp_path: Path) -> None:
    # Given a turn the API rejected (unclassified — session is poisoned)
    # and a queued reminder callback
    engine, worker, sent = _engine(tmp_path)
    fired: list[bool] = []

    async def callback() -> None:
        fired.append(True)

    engine._turn_callbacks = [TurnCallbacks(on_success=callback)]
    result = TurnResult(
        text_blocks=[POLICY_ERROR], dropped_text=True, api_error=POLICY_ERROR
    )

    # When the engine processes the turn result
    await engine._handle_turn_result(result)

    # Then the user is told and the session is respawned fresh —
    # no corrective retry into the poisoned session
    assert len(sent) == 1, "exactly one notification per failed turn"
    chat_id, text = sent[0]
    assert chat_id == -100, "the waiting chat must be notified"
    assert "fresh session" in text, "user must learn the context was cleared"
    assert "Usage Policy" in text, "the API's own diagnostic must be included"
    worker.reset_session.assert_awaited_once()
    # No dropped-text retry into a dead session:
    worker.send.assert_not_awaited()
    assert fired == [True], (
        "callbacks must fire — retrying identical content fails deterministically"
    )
    assert not engine._is_processing.is_set(), "engine must be idle again"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"


@pytest.mark.asyncio
async def test_transient_error_notifies_without_reset(tmp_path: Path) -> None:
    # Given a turn that failed with a classified transient error
    engine, worker, sent = _engine(tmp_path)
    result = TurnResult(api_error="API Error: 429 rate limit exceeded")

    # When the engine processes the turn result
    await engine._handle_turn_result(result)

    # Then the targeted message is sent and the session SURVIVES —
    # a reset would lose context without fixing anything
    assert len(sent) == 1, "exactly one notification per failed turn"
    assert "rate-limited" in sent[0][1], "targeted rate-limit message expected"
    worker.reset_session.assert_not_awaited()
    assert not engine._is_processing.is_set(), "engine must be idle again"


# ----------------------------------------------------------------------
# Engine: restored-context digest on the api-error reset
# ----------------------------------------------------------------------


def _row(mid: int, text: str) -> ChatMessage:
    return ChatMessage(
        chat_id=-100,
        message_id=mid,
        user_id=42,
        username="alice",
        first_name="Alice",
        direction="in",
        timestamp=datetime(2026, 6, 12, 10, mid % 60, tzinfo=timezone.utc),
        text=text,
    )


@pytest.mark.asyncio
async def test_api_error_reset_stashes_digest_excluding_failed_batch(
    tmp_path: Path,
) -> None:
    # Given committed history plus the (never-committed) batch of the
    # turn the API just rejected
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        await insert_message(db, _row(1, "earlier question"))
        await mark_messages_consumed(db, [(-100, 1)])
        await mark_messages_processed(db, [(-100, 1)])
        await insert_message(db, _row(2, "the poisoned request"))
        await mark_messages_consumed(db, [(-100, 2)])

        engine, worker, sent = _engine(tmp_path, db)

        # When the unclassified API error arrives
        await engine._handle_turn_result(TurnResult(api_error=POLICY_ERROR))

        # Then the next fresh turn carries a digest WITHOUT the failed
        # batch — its rows were never committed, so the trust filter
        # drops them with no in-memory bookkeeping
        assert engine._restore_context is not None, "digest must be stashed"
        assert "earlier question" in engine._restore_context
        assert "the poisoned request" not in engine._restore_context, (
            "the failed batch is the likeliest poison — it must be excluded"
        )
        assert "recap" in sent[0][1], "user must learn context is carried over"
        worker.reset_session.assert_awaited_once()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_poisoned_digest_guard_forces_plain_session(tmp_path: Path) -> None:
    # Given a turn that itself opened with a restored digest and failed
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        await insert_message(db, _row(1, "some history"))
        await mark_messages_consumed(db, [(-100, 1)])
        await mark_messages_processed(db, [(-100, 1)])
        engine, worker, sent = _engine(tmp_path, db)
        engine._turn.had_restored_context = True

        # When the API error arrives
        await engine._handle_turn_result(TurnResult(api_error=POLICY_ERROR))

        # Then no new digest is built — the digest is the prime suspect —
        # and the user is told the recap could not be carried over
        assert engine._restore_context is None, (
            "one-shot guard: a poisoned digest must never loop"
        )
        assert "could not be carried over" in sent[0][1]
        worker.reset_session.assert_awaited_once()
    finally:
        await db.close()
