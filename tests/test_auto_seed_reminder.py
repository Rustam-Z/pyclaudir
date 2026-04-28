"""Startup seeding of default reminders.

Verifies:
- First startup inserts exactly one row with the self-reflection auto_seed_key.
- Subsequent startups don't duplicate.
- A cancelled row still blocks re-seeding (cancel-sticky semantics).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.__main__ import _seed_default_reminders
from pyclaudir.config import Config
from pyclaudir.db.database import Database


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


def _cfg(tmp_path: Path, owner_id: int = 42) -> Config:
    cfg = Config.for_test(tmp_path)
    object.__setattr__(cfg, "owner_id", owner_id)
    return cfg


@pytest.mark.asyncio
async def test_first_startup_seeds_self_reflection(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await _seed_default_reminders(db, _cfg(tmp_path))
        rows = await db.fetch_all(
            "SELECT chat_id, user_id, text, cron_expr, status, auto_seed_key "
            "FROM reminders WHERE auto_seed_key = 'self-reflection-default'"
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["chat_id"] == 42
        assert r["user_id"] == -1
        assert r["text"] == '<skill name="self-reflection">run</skill>'
        assert r["cron_expr"] == "0 0 * * *"
        assert r["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_second_startup_does_not_duplicate(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await _seed_default_reminders(db, _cfg(tmp_path))
        await _seed_default_reminders(db, _cfg(tmp_path))
        rows = await db.fetch_all(
            "SELECT id FROM reminders WHERE auto_seed_key = 'self-reflection-default'"
        )
        assert len(rows) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_deleted_row_triggers_reseed(tmp_path: Path) -> None:
    """If the reminder row is DELETEd entirely (manual SQL, bad actor,
    DB corruption), the next startup re-seeds it. There is no way to
    make the self-reflection loop stay gone."""
    db = await _open(tmp_path)
    try:
        await _seed_default_reminders(db, _cfg(tmp_path))
        await db.execute(
            "DELETE FROM reminders WHERE auto_seed_key = 'self-reflection-default'"
        )
        rows = await db.fetch_all(
            "SELECT id FROM reminders WHERE auto_seed_key = 'self-reflection-default'"
        )
        assert len(rows) == 0

        # Simulate a restart.
        await _seed_default_reminders(db, _cfg(tmp_path))

        rows = await db.fetch_all(
            "SELECT id, status FROM reminders "
            "WHERE auto_seed_key = 'self-reflection-default'"
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancelled_row_triggers_reseed(tmp_path: Path) -> None:
    """'Learning cannot be stopped' — if the self-reflection reminder is
    cancelled (by any means, including manual SQL), the next startup
    re-seeds a fresh pending row. The old cancelled row is left alone
    as historical record."""
    db = await _open(tmp_path)
    try:
        await _seed_default_reminders(db, _cfg(tmp_path))
        first = await db.fetch_one(
            "SELECT id FROM reminders WHERE auto_seed_key = 'self-reflection-default'"
        )
        assert first is not None
        # Cancel via direct SQL (simulates a bad actor or manual tampering).
        await db.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ?",
            (int(first["id"]),),
        )

        # Simulate a restart.
        await _seed_default_reminders(db, _cfg(tmp_path))

        rows = await db.fetch_all(
            "SELECT id, status FROM reminders "
            "WHERE auto_seed_key = 'self-reflection-default' ORDER BY id"
        )
        assert len(rows) == 2
        assert rows[0]["status"] == "cancelled"
        assert rows[1]["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reminder_tool_refuses_to_cancel_auto_seeded(tmp_path: Path) -> None:
    """Hard gate at the tool layer — cancel_reminder refuses auto-seeded
    rows, so even if the bot is prompt-injected into calling it, it
    cannot stop the self-reflection loop."""
    from pyclaudir.tools.base import ToolContext
    from pyclaudir.tools.reminder import CancelReminderArgs, CancelReminderTool

    db = await _open(tmp_path)
    try:
        await _seed_default_reminders(db, _cfg(tmp_path))
        row = await db.fetch_one(
            "SELECT id FROM reminders WHERE auto_seed_key = 'self-reflection-default'"
        )
        assert row is not None

        ctx = ToolContext(database=db)
        result = await CancelReminderTool(ctx).run(
            CancelReminderArgs(reminder_id=int(row["id"]))
        )
        assert result.is_error is True
        assert "auto-seeded mandatory loop" in result.content
        assert "self-reflection-default" in result.content

        # Verify nothing was cancelled.
        after = await db.fetch_one(
            "SELECT status FROM reminders WHERE id = ?", (int(row["id"]),)
        )
        assert after["status"] == "pending"
    finally:
        await db.close()
