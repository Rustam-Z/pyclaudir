"""Persistence helpers for the ``reminders`` table."""

from __future__ import annotations

from datetime import datetime, timezone

from .database import Database


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def insert_reminder(
    db: Database,
    *,
    chat_id: int,
    user_id: int,
    text: str,
    trigger_at: str,
    cron_expr: str | None = None,
) -> int:
    """Insert a new reminder and return its id."""
    cursor = await db.connection.execute(
        """
        INSERT INTO reminders (chat_id, user_id, text, trigger_at, cron_expr, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (chat_id, user_id, text, trigger_at, cron_expr, _utcnow_iso()),
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
    """Update trigger_at for a recurring reminder to the next occurrence."""
    await db.execute(
        "UPDATE reminders SET trigger_at = ? WHERE id = ?",
        (next_trigger_at, reminder_id),
    )


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
