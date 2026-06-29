"""Persistence helpers for the ``reminders`` table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .database import Database


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class NewReminder:
    """The columns shared by both reminder-insert paths."""

    chat_id: int
    user_id: int
    text: str
    trigger_at: str
    cron_expr: str | None = None


async def insert_reminder(db: Database, reminder: NewReminder) -> int:
    """Insert a new reminder and return its id."""
    cursor = await db.connection.execute(
        """
        INSERT INTO reminders (chat_id, user_id, text, trigger_at, cron_expr, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            reminder.chat_id,
            reminder.user_id,
            reminder.text,
            reminder.trigger_at,
            reminder.cron_expr,
            _utcnow_iso(),
        ),
    )
    await db.connection.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def fetch_due_reminders(db: Database, now_utc: str) -> list:
    """Return all pending reminders whose trigger_at <= now_utc."""
    return await db.fetch_all(
        "SELECT * FROM reminders WHERE status = 'pending' AND trigger_at <= ?",
        (now_utc,),
    )


async def mark_reminder_sent(db: Database, reminder_id: int) -> None:
    """Mark a one-shot reminder as sent."""
    await db.execute(
        "UPDATE reminders SET status = 'sent' WHERE id = ?",
        (reminder_id,),
    )


async def advance_recurring_reminder(
    db: Database, reminder_id: int, next_trigger_at: str
) -> None:
    """Re-arm a recurring reminder for its next occurrence.

    Sets the next ``trigger_at`` and returns the row to ``pending`` (it
    was ``processing`` while in flight) so the scheduler picks it up at
    the next slot rather than re-firing immediately.
    """
    await db.execute(
        "UPDATE reminders SET trigger_at = ?, status = 'pending' WHERE id = ?",
        (next_trigger_at, reminder_id),
    )


async def claim_reminder(db: Database, reminder_id: int) -> bool:
    """Atomically claim a pending reminder for delivery.

    Flips ``pending`` -> ``processing`` so the next poll's
    :func:`fetch_due_reminders` skips it (that query only returns
    ``pending`` rows). Returns True if this call won the row. The atomic
    guard also stops a second poll tick from re-claiming a reminder that
    is already in flight — the root cause of #44 / #48.
    """
    cursor = await db.connection.execute(
        "UPDATE reminders SET status = 'processing' WHERE id = ? AND status = 'pending'",
        (reminder_id,),
    )
    await db.connection.commit()
    return cursor.rowcount > 0


async def revert_reminder(db: Database, reminder_id: int) -> None:
    """Return an in-flight reminder to ``pending`` so it re-fires.

    Called when the turn delivering the reminder failed before CC
    consumed it (subprocess crash, owner session reset). Preserves the
    #22 'stay pending on failure' guarantee now that the row is claimed.
    """
    await db.execute(
        "UPDATE reminders SET status = 'pending' WHERE id = ? AND status = 'processing'",
        (reminder_id,),
    )


async def reset_stuck_reminders(db: Database) -> int:
    """Re-arm reminders left ``processing`` by a crash or shutdown.

    Run once at startup: nothing can be in flight immediately after boot,
    so any ``processing`` row is a delivery that never finished. Returns
    the number of rows re-armed.
    """
    cursor = await db.connection.execute(
        "UPDATE reminders SET status = 'pending' WHERE status = 'processing'",
    )
    await db.connection.commit()
    return int(cursor.rowcount)


async def cancel_reminder(db: Database, reminder_id: int) -> bool:
    """Cancel a pending reminder. Returns True if a row was updated."""
    cursor = await db.connection.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
        (reminder_id,),
    )
    await db.connection.commit()
    return cursor.rowcount > 0


async def list_pending_reminders(db: Database, chat_id: int) -> list:
    """Return all pending reminders for a given chat."""
    return await db.fetch_all(
        "SELECT * FROM reminders WHERE chat_id = ? AND status = 'pending' ORDER BY trigger_at",
        (chat_id,),
    )


async def pending_with_auto_seed_key(db: Database, key: str) -> int:
    """Count PENDING reminders tagged with the given auto_seed_key.

    Used by the startup seed hook. Only pending rows count as "exists"
    — a cancelled or sent row means the reminder is not currently
    active and the startup hook should re-seed. This is the "learning
    cannot be stopped" guarantee: even if something (bot, operator,
    manual SQL) cancels the self-reflection reminder, the next restart
    re-seeds it.
    """
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM reminders WHERE auto_seed_key = ? AND status = 'pending'",
        (key,),
    )
    return int(row["c"]) if row is not None else 0


async def cancel_auto_seeded(db: Database, key: str) -> int:
    """Cancel pending reminders tagged with the given auto_seed_key.

    Counterpart to :func:`pending_with_auto_seed_key`. Used when an
    auto-seeded loop (currently self-reflection) is switched off by the
    operator so the existing pending row stops firing. Returns the number
    of rows cancelled.
    """
    cursor = await db.connection.execute(
        "UPDATE reminders SET status = 'cancelled' "
        "WHERE auto_seed_key = ? AND status = 'pending'",
        (key,),
    )
    await db.connection.commit()
    return int(cursor.rowcount)


async def fetch_reminder_by_id(db: Database, reminder_id: int) -> dict | None:
    """Fetch a single reminder by id, or None if not found."""
    row = await db.fetch_one(
        "SELECT id, chat_id, user_id, text, trigger_at, cron_expr, status, "
        "auto_seed_key FROM reminders WHERE id = ?",
        (reminder_id,),
    )
    if row is None:
        return None
    return dict(row)


async def insert_auto_seeded_reminder(
    db: Database, reminder: NewReminder, auto_seed_key: str
) -> int:
    """Insert a default reminder tagged with an ``auto_seed_key``.

    Same columns as :func:`insert_reminder` plus the ``auto_seed_key``
    so future startup checks can see this row already exists.
    """
    cursor = await db.connection.execute(
        """
        INSERT INTO reminders (
            chat_id, user_id, text, trigger_at, cron_expr,
            status, created_at, auto_seed_key
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            reminder.chat_id,
            reminder.user_id,
            reminder.text,
            reminder.trigger_at,
            reminder.cron_expr,
            _utcnow_iso(),
            auto_seed_key,
        ),
    )
    await db.connection.commit()
    return cursor.lastrowid  # type: ignore[return-value]
