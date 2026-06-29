"""The claim lifecycle that stops reminders re-firing every minute (#44, #48).

A due reminder is claimed (``pending`` -> ``processing``) the moment it is
fired, so the next poll's :func:`fetch_due_reminders` skips it instead of
re-firing one that is still in flight. The claim is released three ways:
advance/close on success, revert on failure, and a boot reset for rows a
crash left stuck in ``processing``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hamroh.config import Config
from hamroh.db.database import Database
from hamroh.db.reminders import (
    NewReminder,
    advance_recurring_reminder,
    claim_reminder,
    fetch_due_reminders,
    insert_reminder,
    reset_stuck_reminders,
    revert_reminder,
)

#: A trigger in the deep past and a "now" far in the future, so an unclaimed
#: reminder is always due and string-comparison in fetch_due_reminders holds.
_PAST = "2000-01-01 00:00:00"
_NOW = "2100-01-01 00:00:00"


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


async def _insert_due(db: Database, *, cron_expr: str | None = None) -> int:
    return await insert_reminder(
        db,
        NewReminder(
            chat_id=-100,
            user_id=42,
            text="ping",
            trigger_at=_PAST,
            cron_expr=cron_expr,
        ),
    )


@pytest.mark.asyncio
async def test_claim_hides_reminder_from_next_poll(tmp_path: Path) -> None:
    """given  a pending reminder that is due
    when    it is claimed
    then    the next poll no longer selects it — the root cause of #44/#48.
    """
    db = await _open(tmp_path)
    try:
        rid = await _insert_due(db)
        due = await fetch_due_reminders(db, _NOW)
        assert [r["id"] for r in due] == [rid], (
            "a pending due reminder must be selected"
        )

        won = await claim_reminder(db, rid)
        assert won is True, "claiming a pending reminder must succeed"

        after = await fetch_due_reminders(db, _NOW)
        assert after == [], "a claimed (processing) reminder must not be re-selected"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_claim_is_idempotent(tmp_path: Path) -> None:
    """given  a reminder already claimed for delivery
    when    a second poll tries to claim it
    then    the claim fails — the row can't be fired twice while in flight.
    """
    db = await _open(tmp_path)
    try:
        rid = await _insert_due(db)
        assert await claim_reminder(db, rid) is True, "first claim must win the row"
        assert await claim_reminder(db, rid) is False, (
            "a second claim must not re-win an in-flight reminder"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_revert_re_arms_a_claimed_reminder(tmp_path: Path) -> None:
    """given  a claimed reminder whose delivery failed
    when    the claim is reverted (the on_failure path)
    then    it is due again for the next loop tick (#22 at-least-once).
    """
    db = await _open(tmp_path)
    try:
        rid = await _insert_due(db)
        await claim_reminder(db, rid)
        await revert_reminder(db, rid)

        due = await fetch_due_reminders(db, _NOW)
        assert [r["id"] for r in due] == [rid], "a reverted reminder must be due again"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_boot_reset_re_arms_stuck_reminders(tmp_path: Path) -> None:
    """given  a reminder a crash left stuck in 'processing'
    when    the bot reboots and runs reset_stuck_reminders
    then    the row is re-armed to pending and due again.
    """
    db = await _open(tmp_path)
    try:
        rid = await _insert_due(db)
        await claim_reminder(db, rid)

        count = await reset_stuck_reminders(db)
        assert count == 1, "exactly one stuck reminder should be re-armed"

        due = await fetch_due_reminders(db, _NOW)
        assert [r["id"] for r in due] == [rid], (
            "the re-armed reminder must be due again"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_advance_recurring_returns_row_to_pending(tmp_path: Path) -> None:
    """given  a claimed recurring reminder that delivered cleanly
    when    its schedule is advanced to the next slot
    then    it returns to pending at the next trigger, not due until then.
    """
    db = await _open(tmp_path)
    try:
        rid = await _insert_due(db, cron_expr="0 0 * * *")
        await claim_reminder(db, rid)
        await advance_recurring_reminder(db, rid, "2100-06-18 00:00:00")

        row = await db.fetch_one(
            "SELECT status, trigger_at FROM reminders WHERE id = ?", (rid,)
        )
        assert row is not None
        assert row["status"] == "pending", "advanced reminder must return to pending"
        assert row["trigger_at"] == "2100-06-18 00:00:00", (
            "trigger must move to next slot"
        )

        due = await fetch_due_reminders(db, _NOW)
        assert due == [], "the advanced reminder must not be due until its next slot"
    finally:
        await db.close()
