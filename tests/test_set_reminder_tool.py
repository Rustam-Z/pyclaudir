"""SetReminderTool — trigger_at format normalization.

Regression: the tool used to pass ``args.trigger_at`` straight through
to the DB. ISO-8601 with ``T`` and ``Z`` (e.g. ``2026-04-30T22:40:53Z``)
sorts lexicographically *higher* than the ``"%Y-%m-%d %H:%M:%S"`` format
that ``fetch_due_reminders`` compares against (``T`` > space), so a
user-set one-shot reminder was never picked up by the polling loop.
The tool now normalizes to the canonical storage format on insert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pyclaudir.config import Config
from pyclaudir.db.database import Database
from pyclaudir.db.reminders import fetch_due_reminders
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.reminder import SetReminderArgs, SetReminderTool


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


@pytest.mark.asyncio
async def test_iso_trigger_at_is_stored_canonical(tmp_path: Path) -> None:
    """A trigger_at like '2026-...T...Z' must be stored as
    '2026-... ...' so SQL string-compare against the loop's now-string
    works."""
    db = await _open(tmp_path)
    try:
        tool = SetReminderTool(ToolContext(database=db))
        future = datetime.now(timezone.utc) + timedelta(minutes=2)
        iso_with_z = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        result = await tool.run(SetReminderArgs(
            chat_id=42, user_id=42, text="hi", trigger_at=iso_with_z,
        ))
        assert not result.is_error, result.content
        row = await db.fetch_one("SELECT trigger_at FROM reminders WHERE chat_id = 42")
        assert row is not None
        assert "T" not in row["trigger_at"]
        assert "Z" not in row["trigger_at"]
        assert row["trigger_at"] == future.strftime("%Y-%m-%d %H:%M:%S")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_due_reminder_is_picked_up_by_loop_query(tmp_path: Path) -> None:
    """End-to-end: after the trigger time, fetch_due_reminders must
    return the row inserted by the tool. This is what was silently
    broken before — the loop polled but the SQL never matched."""
    db = await _open(tmp_path)
    try:
        tool = SetReminderTool(ToolContext(database=db))
        future = datetime.now(timezone.utc) + timedelta(seconds=1)
        await tool.run(SetReminderArgs(
            chat_id=42, user_id=42, text="hi",
            trigger_at=future.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))
        # Pretend we polled 5 minutes after the trigger.
        later = future + timedelta(minutes=5)
        due = await fetch_due_reminders(db, later.strftime("%Y-%m-%d %H:%M:%S"))
        assert len(due) == 1
        assert due[0]["chat_id"] == 42
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_naive_trigger_at_is_rejected(tmp_path: Path) -> None:
    """ISO-8601 without a timezone is ambiguous and must be refused —
    the schema docs require UTC with a 'Z' (or explicit offset)."""
    db = await _open(tmp_path)
    try:
        tool = SetReminderTool(ToolContext(database=db))
        future_naive = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%S")  # no Z, no offset
        result = await tool.run(SetReminderArgs(
            chat_id=42, user_id=42, text="hi", trigger_at=future_naive,
        ))
        assert result.is_error
        assert "timezone" in result.content.lower()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_offset_trigger_at_is_normalized_to_utc(tmp_path: Path) -> None:
    """A trigger_at with a non-UTC offset (e.g. +02:00) must still
    land in the DB as a UTC '%Y-%m-%d %H:%M:%S' string, so all
    comparisons work."""
    db = await _open(tmp_path)
    try:
        tool = SetReminderTool(ToolContext(database=db))
        # Construct: 2099-01-01 12:00 in +02:00 = 2099-01-01 10:00 UTC
        iso_offset = "2099-01-01T12:00:00+02:00"
        result = await tool.run(SetReminderArgs(
            chat_id=42, user_id=42, text="hi", trigger_at=iso_offset,
        ))
        assert not result.is_error, result.content
        row = await db.fetch_one("SELECT trigger_at FROM reminders WHERE chat_id = 42")
        assert row is not None
        assert row["trigger_at"] == "2099-01-01 10:00:00"
    finally:
        await db.close()
