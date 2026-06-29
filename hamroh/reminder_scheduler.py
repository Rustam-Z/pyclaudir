"""Reminder scheduler — the background loop that fires due reminders.

Polls the ``reminders`` table every 60s and injects each due reminder into
the engine as a synthetic message. Persistence lives in
:mod:`hamroh.db.reminders` and the agent-facing tools in
:mod:`hamroh.tools.reminder`; this module is the runtime glue between
them — claim a due reminder, inject it, and on the engine's success/failure
hook advance/close it or revert the claim (#22, #44, #48).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .db.database import Database
from .db.reminders import (
    advance_recurring_reminder,
    claim_reminder,
    fetch_due_reminders,
    mark_reminder_sent,
    revert_reminder,
)
from .engine import Engine

log = logging.getLogger("hamroh.reminder")


async def _advance_or_close_reminder(db: Database, row: dict) -> None:
    """For a fired reminder: advance the cron schedule if recurring,
    otherwise mark it sent so it doesn't fire again."""
    cron_expr = row["cron_expr"]
    if not cron_expr:
        await mark_reminder_sent(db, row["id"])
        return
    try:
        from croniter import croniter

        next_dt = croniter(
            cron_expr,
            datetime.now(timezone.utc),
        ).get_next(datetime)
        await advance_recurring_reminder(
            db,
            row["id"],
            next_dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except ImportError:
        log.warning(
            "croniter not installed, marking cron reminder #%d as sent",
            row["id"],
        )
        await mark_reminder_sent(db, row["id"])


def _make_reminder_callbacks(
    db: Database, row: dict
) -> tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]]:
    """Build the engine success/failure hooks for a fired reminder.

    ``on_success`` commits the reminder once CC has processed the turn —
    advance the cron schedule or close a one-shot. ``on_failure`` reverts
    the claim so a crash or owner session reset before CC consumed the
    reminder XML leaves the row claimable again on the next loop tick (#22).
    """

    async def _on_success() -> None:
        await _advance_or_close_reminder(db, row)
        log.info("delivered reminder #%d", row["id"])

    async def _on_failure() -> None:
        await revert_reminder(db, row["id"])
        log.info("reverted reminder #%d after failed delivery", row["id"])

    return _on_success, _on_failure


async def _fire_one_reminder(db: Database, engine: Engine, row: dict) -> None:
    """Claim one due reminder and inject it as a synthetic message.

    The claim (``pending`` -> ``processing``) stops the next poll from
    re-firing a reminder still being delivered (#44, #48). ``on_success``
    advances/closes it after CC consumes the turn; ``on_failure`` reverts
    the claim so a crash/reset before then re-fires it next tick (#22).
    """
    from .models import ChatMessage

    if not await claim_reminder(db, row["id"]):
        log.info("reminder #%d already claimed; skipping", row["id"])
        return

    reminder_xml = (
        f'<reminder id="{row["id"]}" chat_id="{row["chat_id"]}" '
        f'user_id="{row["user_id"]}">{row["text"]}</reminder>'
    )
    on_success, on_failure = _make_reminder_callbacks(db, row)
    try:
        await engine.submit(
            ChatMessage(
                chat_id=row["chat_id"],
                message_id=0,
                user_id=row["user_id"],
                direction="in",
                timestamp=datetime.now(timezone.utc),
                text=reminder_xml,
            ),
            on_success=on_success,
            on_failure=on_failure,
        )
    except Exception:
        # The submit never registered the hooks, so on_failure won't run —
        # release the claim here so the next loop tick can retry (#22).
        await revert_reminder(db, row["id"])
        raise


async def _reminder_loop(db: Database, engine: Engine) -> None:
    """Background reminder scheduler — polls every 60s for due reminders
    and injects them into the engine as synthetic inbound messages.

    Reminders fire unconditionally when due. If the engine is mid-turn
    the synthetic message gets buffered and runs after the current
    turn ends. Each reminder is fired in its own try/except so a single
    failure (DB error, submit blow-up) doesn't block subsequent
    reminders in the same poll cycle, and the failing row's id is
    logged so it's easy to track down.
    """
    while True:
        await asyncio.sleep(60)
        try:
            now_dt = datetime.now(timezone.utc)
            due = await fetch_due_reminders(
                db,
                now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:
            log.exception("reminder loop: fetch_due_reminders failed")
            continue
        if not due:
            log.debug("reminder loop: no due reminders")
            continue
        log.info("reminder loop: %d due", len(due))
        for row in due:
            try:
                log.info(
                    "firing reminder #%d (chat=%s)",
                    row["id"],
                    row["chat_id"],
                )
                await _fire_one_reminder(db, engine, row)
                # The row is now ``processing`` (claimed), so the next poll
                # skips it. It returns to ``pending`` only if delivery fails
                # (on_failure) — see ``_fire_one_reminder``.
                log.info("queued reminder #%d", row["id"])
            except Exception:
                log.exception("failed to fire reminder #%d", row["id"])
