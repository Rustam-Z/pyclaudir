"""Entrypoint: ``python -m pyclaudir``.

Brings up the four components in order:

1. SQLite database (with migrations applied)
2. Local MCP server on a random localhost port
3. Claude Code subprocess via the CC worker
4. Engine + Telegram dispatcher

Then sleeps until interrupted, at which point everything is torn down.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .cc_worker import CcWorker
from .config import Config
from .db.database import Database
from .db.reminders import (
    advance_recurring_reminder,
    fetch_due_reminders,
    mark_reminder_sent,
)
from .engine import Engine
from .startup import (
    _acquire_instance_lock,
    _App,
    _bootstrap_access,
    _build_cc_spec,
    _build_dispatcher_and_engine,
    _make_on_cc_crash,
    _make_on_cc_giveup,
    _make_on_cc_stale_session,
    _open_db_and_stores,
    _replay_unconsumed,
    _run_until_stopped,
    _seed_default_reminders,
    _setup_logging,
    _start_mcp_server,
)

__all__ = ["main", "_seed_default_reminders"]

log = logging.getLogger("pyclaudir")


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
            cron_expr, datetime.now(timezone.utc),
        ).get_next(datetime)
        await advance_recurring_reminder(
            db, row["id"], next_dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except ImportError:
        log.warning(
            "croniter not installed, marking cron reminder #%d as sent",
            row["id"],
        )
        await mark_reminder_sent(db, row["id"])


def _make_reminder_on_success(db: Database, row: dict):
    """Build the engine ``on_success`` hook that commits a reminder
    once CC has actually processed the turn (#22)."""

    async def _on_success() -> None:
        await _advance_or_close_reminder(db, row)
        log.info("delivered reminder #%d", row["id"])

    return _on_success


async def _fire_one_reminder(db: Database, engine: Engine, row: dict) -> None:
    """Inject one due reminder into the engine as a synthetic message.

    The schedule advance / one-shot close is deferred to an
    ``on_success`` callback the engine fires after CC actually consumes
    the turn. If the CC subprocess crashes or wedges before processing
    the reminder XML, the callback never fires and the row stays
    ``pending`` — the next 60s reminder loop tick re-fires it. Without
    this, a wedged subprocess would silently drop the reminder (#22).

    If the engine is mid-turn when the reminder fires, the synthetic
    message goes into the pending buffer and runs after the current
    turn ends.
    """
    from .models import ChatMessage

    reminder_xml = (
        f'<reminder id="{row["id"]}" chat_id="{row["chat_id"]}" '
        f'user_id="{row["user_id"]}">{row["text"]}</reminder>'
    )
    await engine.submit(
        ChatMessage(
            chat_id=row["chat_id"],
            message_id=0,
            user_id=row["user_id"],
            direction="in",
            timestamp=datetime.now(timezone.utc),
            text=reminder_xml,
        ),
        on_success=_make_reminder_on_success(db, row),
    )


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
                db, now_dt.strftime("%Y-%m-%d %H:%M:%S"),
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
                    "firing reminder #%d (chat=%s)", row["id"], row["chat_id"],
                )
                await _fire_one_reminder(db, engine, row)
                # NB: not "fired" — the row stays ``pending`` until the
                # engine's on_success callback runs after CC processes
                # the turn. See ``_fire_one_reminder`` for the rationale.
                log.info("queued reminder #%d", row["id"])
            except Exception:
                log.exception("failed to fire reminder #%d", row["id"])


async def _async_main() -> None:
    _setup_logging()
    config = Config.from_env()
    config.ensure_dirs()
    lock = _acquire_instance_lock(config)  # refuse to boot twice on one data dir
    _bootstrap_access(config)

    db, plugins, stores = await _open_db_and_stores(config)
    app = _App(config=config, db=db, lock=lock)

    chat_titles: dict[int, str] = {}  # dispatcher writes, outbound tools read
    ctx, app.mcp = await _start_mcp_server(db, stores, plugins, chat_titles)
    spec = _build_cc_spec(config, plugins, app.mcp)

    app.worker = CcWorker(
        spec, config,
        heartbeat=ctx.heartbeat,
        on_crash=_make_on_cc_crash(app),
        on_giveup=_make_on_cc_giveup(app),
        on_stale_session=_make_on_cc_stale_session(app),
    )
    await app.worker.start()
    await app.worker.supervise()

    app.dispatcher, app.engine = _build_dispatcher_and_engine(
        app, stores, chat_titles,
    )
    await app.engine.start()
    await _replay_unconsumed(db, app.engine)
    app.reminder_task = asyncio.create_task(
        _reminder_loop(db, app.engine), name="pyclaudir-reminders",
    )

    app.dispatcher.engine = app.engine
    ctx.bot = app.dispatcher.bot
    ctx.on_chat_replied = app.engine.notify_chat_replied  # stops typing on reply
    await app.dispatcher.start()
    log.info("pyclaudir is live")

    await _run_until_stopped(app)


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
