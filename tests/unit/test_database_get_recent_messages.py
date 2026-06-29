"""Unit tests for the ``database_get_recent_messages`` tool."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hamroh.config import Config
from hamroh.db.database import Database
from hamroh.db.messages import insert_message, mark_deleted
from hamroh.models import ChatMessage
from hamroh.tools.base import ToolContext
from hamroh.tools.database_get_recent_messages import (
    DatabaseGetRecentMessagesArgs,
    DatabaseGetRecentMessagesTool,
)

_T = datetime(2026, 4, 11, 10, 31, tzinfo=timezone.utc)


async def _insert(
    db: Database, message_id: int, text: str, *, direction: str = "in"
) -> None:
    """Insert one message; new inbound rows stay ``processed=0`` by default."""
    await insert_message(
        db,
        ChatMessage(
            chat_id=-1,
            message_id=message_id,
            user_id=42,
            direction=direction,
            timestamp=_T,
            text=text,
        ),
    )


async def _run(
    db: Database | None,
    limit: int = 20,
    *,
    chat_id: int | None = None,
    before_message_id: int | None = None,
):
    """Run the tool against ``db`` and return its ``ToolResult``."""
    tool = DatabaseGetRecentMessagesTool(ToolContext(database=db))
    return await tool.run(
        DatabaseGetRecentMessagesArgs(
            limit=limit, chat_id=chat_id, before_message_id=before_message_id
        )
    )


async def _insert_in_chat(
    db: Database, chat_id: int, message_id: int, text: str
) -> None:
    """Insert one inbound message into a specific chat."""
    await insert_message(
        db,
        ChatMessage(
            chat_id=chat_id,
            message_id=message_id,
            user_id=42,
            direction="in",
            timestamp=_T,
            text=text,
        ),
    )


@pytest.mark.asyncio
async def test_returns_recent_messages_oldest_first(tmp_path: Path) -> None:
    # Given three messages inserted in order
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        for i in (1, 2, 3):
            await _insert(db, i, f"msg-{i}")

        # When the model asks for recent messages
        result = await _run(db)

        # Then they come back oldest-first under a TSV header
        assert result.is_error is False, result.content
        lines = result.content.splitlines()
        assert lines[0].startswith("timestamp\tdirection"), "missing TSV header"
        bodies = [ln.split("\t")[-1] for ln in lines[1:]]
        assert bodies == ["msg-1", "msg-2", "msg-3"], f"wrong order: {bodies}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_includes_unprocessed_inbound(tmp_path: Path) -> None:
    # Given a freshly inserted inbound message (processed=0, the in-flight turn)
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        await _insert(db, 1, "live-inbound")

        # When the model recalls recent messages
        result = await _run(db)

        # Then the unprocessed inbound row is visible (unlike the restore digest)
        assert "live-inbound" in result.content, (
            "tool must show the current turn's own inbound messages"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_excludes_deleted_and_synthetic_rows(tmp_path: Path) -> None:
    # Given a real message, a deleted message, and a synthetic (id<=0) row
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        await _insert(db, 1, "keep-me")
        await _insert(db, 2, "delete-me")
        await mark_deleted(db, -1, 2)
        await _insert(db, 0, "synthetic-reminder")

        # When the model recalls recent messages
        result = await _run(db)

        # Then only the real, non-deleted message survives
        assert "keep-me" in result.content, "real message must be returned"
        assert "delete-me" not in result.content, "deleted rows must be skipped"
        assert "synthetic-reminder" not in result.content, (
            "synthetic rows (message_id <= 0) must be skipped"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_clamps_oversized_limit(tmp_path: Path) -> None:
    # Given more messages than the hard cap
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        for i in range(1, 151):
            await _insert(db, i, f"msg-{i}")

        # When the caller asks for more than MAX_LIMIT rows
        result = await _run(db, limit=500)

        # Then the result is clamped to the cap
        assert result.is_error is False, result.content
        assert result.data == {"row_count": 100}, "limit must clamp to MAX_LIMIT"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reports_when_database_unavailable() -> None:
    # Given no database wired into the tool context
    # When the model calls the tool
    result = await _run(None)

    # Then it reports the missing database as an error
    assert result.is_error is True, "missing database must be an error"
    assert "database unavailable" in result.content


@pytest.mark.asyncio
async def test_chat_id_restricts_to_one_chat(tmp_path: Path) -> None:
    # Given messages spread across two chats
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        await _insert_in_chat(db, 100, 1, "chat-a")
        await _insert_in_chat(db, 200, 1, "chat-b")

        # When the model scopes the recall to one chat
        result = await _run(db, chat_id=100)

        # Then only that chat's messages come back
        assert "chat-a" in result.content, "the requested chat must be returned"
        assert "chat-b" not in result.content, "other chats must be excluded"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_before_message_id_pages_back(tmp_path: Path) -> None:
    # Given a chat with five messages
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        for mid in (1, 2, 3, 4, 5):
            await _insert_in_chat(db, 100, mid, f"msg-{mid}")

        # When paging back to messages older than id 3
        result = await _run(db, chat_id=100, before_message_id=3)

        # Then only the strictly-older messages return, oldest-first
        bodies = [ln.split("\t")[-1] for ln in result.content.splitlines()[1:]]
        assert bodies == ["msg-1", "msg-2"], f"pagination window wrong: {bodies}"
    finally:
        await db.close()
