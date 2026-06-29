"""Retention of the ``unauthorized_messages`` table (given/when/then)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hamroh.config import Config
from hamroh.db.database import Database
from hamroh.db.unauthorized import RETENTION_DAYS, insert_unauthorized_message
from hamroh.models import ChatMessage


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


def _cm(message_id: int) -> ChatMessage:
    return ChatMessage(
        chat_id=-200,
        message_id=message_id,
        user_id=77,
        username="stranger",
        first_name="Stranger",
        direction="in",
        timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        text="let me in",
    )


@pytest.mark.asyncio
async def test_insert_prunes_rows_older_than_retention(tmp_path: Path) -> None:
    # Given one row older than the retention window and one recent row
    db = await _open(tmp_path)
    try:
        await db.execute(
            "INSERT INTO unauthorized_messages "
            "(chat_id, chat_type, message_id, user_id, timestamp, text, refusal_sent) "
            f"VALUES (-200, 'private', 1, 77, "
            f"datetime('now', '-{RETENTION_DAYS + 1} days'), 'old', 0)"
        )
        await db.execute(
            "INSERT INTO unauthorized_messages "
            "(chat_id, chat_type, message_id, user_id, timestamp, text, refusal_sent) "
            "VALUES (-200, 'private', 2, 77, datetime('now', '-1 day'), 'recent', 0)"
        )

        # When a new unauthorized message is logged
        await insert_unauthorized_message(
            db,
            cm=_cm(3),
            chat_type="private",
            refusal_sent=True,
        )

        # Then the stale row is pruned and both others remain
        rows = await db.fetch_all(
            "SELECT message_id FROM unauthorized_messages ORDER BY message_id"
        )
        ids = [row["message_id"] for row in rows]
        assert ids == [2, 3], f"expected stale row pruned, recent rows kept; got {ids}"
    finally:
        await db.close()
