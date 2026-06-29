"""Entrypoint: ``python -m hamroh``.

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

from .cc_worker import CcWorker, WorkerHooks
from .config import Config
from .reminder_scheduler import _reminder_loop
from .startup import (
    _acquire_instance_lock,
    _App,
    _bootstrap_access,
    _build_cc_spec,
    _build_dispatcher_and_engine,
    _make_on_cc_crash,
    _make_on_cc_giveup,
    _make_on_cc_stale_session,
    _make_on_cc_status,
    _open_db_and_stores,
    _replay_unconsumed,
    _run_until_stopped,
    _seed_default_reminders,
    _start_mcp_server,
)
from .logging_setup import setup_logging

__all__ = ["main", "_seed_default_reminders"]

log = logging.getLogger("hamroh")


async def _async_main() -> None:
    config = Config.from_env()
    config.ensure_dirs()
    setup_logging(config)
    lock = _acquire_instance_lock(config)  # refuse to boot twice on one data dir
    _bootstrap_access(config)

    db, plugins, stores = await _open_db_and_stores(config)
    app = _App(config=config, db=db, lock=lock)

    chat_titles: dict[int, str] = {}  # dispatcher writes, outbound tools read
    ctx, app.mcp = await _start_mcp_server(app, stores, plugins, chat_titles)
    app.browser_manager = ctx.browser_manager
    app.browser_session = ctx.browser_session
    # Warm Chromium in the background so even the first render is fast.
    if app.browser_manager is not None:
        app.warm_task = asyncio.create_task(
            app.browser_manager.warm(),
            name="hamroh-browser-warm",
        )
    spec = _build_cc_spec(config, plugins, app.mcp)
    await _start_worker(app, ctx, spec)
    await _start_engine_and_dispatcher(app, stores, chat_titles, ctx)
    log.info("hamroh is live")

    await _run_until_stopped(app)


async def _start_worker(app: _App, ctx, spec) -> None:
    """Spawn the CC worker wired to the supervisor callbacks, then supervise."""
    app.worker = CcWorker(
        spec,
        app.config,
        WorkerHooks(
            heartbeat=ctx.heartbeat,
            on_crash=_make_on_cc_crash(app),
            on_giveup=_make_on_cc_giveup(app),
            on_stale_session=_make_on_cc_stale_session(app),
            on_status=_make_on_cc_status(app),
        ),
    )
    await app.worker.start()
    await app.worker.supervise()


async def _start_engine_and_dispatcher(app: _App, stores, chat_titles, ctx) -> None:
    """Build + start the engine and dispatcher, replay any unconsumed inbound,
    and arm the reminder loop. The dispatcher goes live last."""
    app.dispatcher, app.engine = _build_dispatcher_and_engine(app, stores, chat_titles)
    await app.engine.start()
    await _replay_unconsumed(app.db, app.engine)
    app.reminder_task = asyncio.create_task(
        _reminder_loop(app.db, app.engine),
        name="hamroh-reminders",
    )
    app.dispatcher.engine = app.engine
    ctx.bot = app.dispatcher.bot
    ctx.on_chat_replied = app.engine.notify_chat_replied  # stops typing on reply
    await app.dispatcher.start()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
