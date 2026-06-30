"""Startup wiring for ``python -m hamroh``.

Everything needed to bring the system up (logging, access bootstrap,
stores, MCP server, CC spawn spec, crash callbacks, dispatcher + engine)
and tear it down again — relocated verbatim from ``__main__.py`` in the
file-size split. ``__main__.py`` keeps the reminder loop and the
``_async_main`` orchestration narrative.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from .access import AccessConfig, load_access, save_access
from .cc_schema import schema_json
from .cc_worker import CcSpawnSpec, CcWorker
from .config import Config
from .db.database import Database
from .db.messages import ToolCall, fetch_unconsumed_inbound, insert_tool_call
from .db.reminders import (
    NewReminder,
    cancel_auto_seeded,
    insert_auto_seeded_reminder,
    pending_with_auto_seed_key,
    reset_stuck_reminders,
)
from .engine import Engine, EngineOptions, ErrorNotify, TypingAction
from .instructions_store import InstructionsStore
from .mcp_server import McpServer
from .plugins import Plugins, load_plugins
from .rate_limiter import RateLimitConfig, RateLimiter
from .skills_store import SkillsStore, render_skills_index
from .storage.attachments import AttachmentStore
from .storage.memory import MemoryStore
from .storage.render import RenderStore
from .telegram_io import DispatcherDeps, TelegramDispatcher
from .tools.base import ToolContext
from .tools.browser import BrowserManager, BrowserSession

# Pinned so log captures keyed on ``"hamroh"`` keep matching after
# the module split.
log = logging.getLogger("hamroh")


_SELF_REFLECTION_KEY = "self-reflection-default"


async def _seed_default_reminders(db, config) -> None:
    """Reconcile the default self-reflection reminder with the config flag.

    The self-reflection loop is opt-in via ``HAMROH_SELF_REFLECTION_ENABLED``
    (off by default). This is an operator-only switch read from the
    environment — the bot can't reach it, and while the loop is on the
    agent can't cancel it (see ``CancelReminderTool``).

    Off: cancel any pending auto-seeded row so it stops firing, then return.
    On: ensure a PENDING row with ``auto_seed_key='self-reflection-default'``
    exists. If it's missing (cancelled, deleted, whatever the reason) we
    re-seed it — defense in depth against DB tampering while enabled.
    """
    if not config.self_reflection_enabled:
        cancelled = await cancel_auto_seeded(db, _SELF_REFLECTION_KEY)
        log.info(
            "self-reflection disabled; cancelled %d pending reminder(s)",
            cancelled,
        )
        return
    await _ensure_self_reflection_seeded(db, config)


async def _ensure_self_reflection_seeded(db, config) -> None:
    """Seed the self-reflection reminder unless a pending row already exists."""
    existing = await pending_with_auto_seed_key(db, _SELF_REFLECTION_KEY)
    if existing > 0:
        log.info(
            "self-reflection reminder: %d pending row(s) active, skipping seed",
            existing,
        )
        return

    cron_expr = config.self_reflection_cron
    # Compute the first trigger time from the cron expression if croniter
    # is available; otherwise default to "now" so the reminder loop will
    # pick it up immediately.
    first_trigger = datetime.now(timezone.utc)
    try:
        from croniter import croniter

        first_trigger = croniter(cron_expr, first_trigger).get_next(datetime)
    except ImportError:  # pragma: no cover
        log.warning(
            "croniter not installed, self-reflection reminder set to trigger now"
        )

    await insert_auto_seeded_reminder(
        db,
        NewReminder(
            chat_id=config.owner_id,
            user_id=-1,  # synthetic pseudo-user (same convention as reminder loop)
            text='<skill name="self-reflection">run</skill>',
            trigger_at=first_trigger.strftime("%Y-%m-%d %H:%M:%S"),
            cron_expr=cron_expr,
        ),
        _SELF_REFLECTION_KEY,
    )
    log.info(
        "seeded default self-reflection reminder (cron=%s, next=%s UTC)",
        cron_expr,
        first_trigger.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _bootstrap_access(config: Config) -> None:
    """First-run access.json seed, then log the resolved policy.

    Default is owner-only DMs with no allowed chats — operator adds
    others later via ``/telegram:access``.
    """
    if not config.access_path.exists():
        seed = AccessConfig(policy="owner_only", allowed_users=[], allowed_chats=[])
        save_access(config.access_path, seed)
        log.info("created %s (policy=owner_only, chats=[])", config.access_path)
        return
    access = load_access(config.access_path)
    log.info(
        "access: policy=%s, allowed_users=%d, allowed_chats=%d",
        access.policy,
        len(access.allowed_users),
        len(access.allowed_chats),
    )


@dataclass
class _Stores:
    """Bundle of long-lived stores constructed at startup. Keeps the
    bootstrap pipeline's signature manageable — the stores are read-only
    after construction and shared across MCP tools, dispatcher, engine."""

    memory: MemoryStore
    instructions: InstructionsStore
    skills: SkillsStore
    attachments: AttachmentStore
    renders: RenderStore
    rate_limiter: RateLimiter


def _build_stores(config: Config, db: Database, plugins: Plugins) -> _Stores:
    """Construct + warm every disk-backed store."""
    project_root = Path(__file__).resolve().parent.parent
    memory = MemoryStore(
        config.memories_dir, committed_root=config.committed_memories_dir
    )
    memory.ensure_root()
    instructions = InstructionsStore(
        project_md_path=project_root / "prompts" / "project.md",
        backup_dir=config.data_dir / "prompt_backups",
    )
    instructions.ensure_dirs()
    skills = SkillsStore(
        root=project_root / "skills",
        disabled=plugins.skills_disabled,
    )
    skills.ensure_root()
    attachments = AttachmentStore(config.attachments_dir)
    renders = RenderStore(config.renders_dir)
    renders.ensure_root()
    rate_limiter = RateLimiter(
        db,
        RateLimitConfig(limit=config.rate_limit_per_min, owner_id=config.owner_id),
    )
    return _Stores(
        memory=memory,
        instructions=instructions,
        skills=skills,
        attachments=attachments,
        renders=renders,
        rate_limiter=rate_limiter,
    )


def _build_external_mcp_config(
    plugins: Plugins,
) -> tuple[dict, list[str]]:
    """Build the MCP-server map + allowed-tool list for the CC subprocess.

    Each enabled entry in ``plugins.json`` whose ``${VAR}`` references all
    resolved contributes one server here. The plugin's ``name`` is the
    dict key Claude Code uses to namespace tools as ``mcp__<name>__<tool>``
    — those names are load-bearing and visible to the model.
    """
    extra_mcp: dict = {}
    mcp_allowed_tools: list[str] = []
    for plugin in plugins.mcps:
        if plugin.type == "stdio":
            extra_mcp[plugin.name] = {
                "type": "stdio",
                "command": plugin.command,
                "args": list(plugin.args),
                "env": dict(plugin.env),
            }
            log.info(
                "mcp %s configured (type=stdio, command=%s)",
                plugin.name,
                plugin.command,
            )
        else:  # http or sse — remote server, optional static auth headers
            entry: dict = {"type": plugin.type, "url": plugin.url}
            if plugin.headers:
                entry["headers"] = dict(plugin.headers)
            extra_mcp[plugin.name] = entry
            log.info(
                "mcp %s configured (type=%s, url=%s)",
                plugin.name,
                plugin.type,
                plugin.url,
            )
        mcp_allowed_tools.extend(plugin.allowed_tools)
    return extra_mcp, mcp_allowed_tools


def _load_session_id(config: Config) -> str | None:
    """Resume the prior CC session if one was persisted on a clean shutdown."""
    if not config.session_id_path.exists():
        return None
    session_id = config.session_id_path.read_text().strip() or None
    if session_id:
        log.info("resuming cc session %s", session_id)
    return session_id


def _install_signal_handlers(
    worker: CcWorker,
    stop_event: asyncio.Event,
) -> None:
    """Wire SIGINT/SIGTERM to the same stop path. Tells the cc supervisor
    we're shutting down BEFORE it observes the subprocess exit (the SIGINT
    propagates to the same process group, so cc is exiting in parallel).
    Without this the supervisor treats the clean exit as a crash and
    respawns."""

    def _stop(*_a) -> None:
        log.info("signal received, shutting down")
        worker._stop_supervisor.set()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)


@dataclass
class _App:
    """Long-lived components, assigned in construction order by
    ``_async_main``. The worker's crash callbacks are built before the
    engine and dispatcher exist and read them from here via late
    binding — by the time the worker invokes a callback, both are wired.
    """

    config: Config
    db: Database
    mcp: McpServer | None = None
    worker: CcWorker | None = None
    dispatcher: TelegramDispatcher | None = None
    engine: Engine | None = None
    reminder_task: asyncio.Task | None = None
    browser_manager: BrowserManager | None = None
    browser_session: BrowserSession | None = None
    #: Background task warming Chromium at boot; held so it isn't GC'd.
    warm_task: asyncio.Task | None = None
    #: Open handle to ``data/.lock`` — held for the process lifetime so
    #: the flock stays alive. Released automatically on any exit.
    lock: IO[str] | None = None


def _acquire_instance_lock(config: Config) -> IO[str]:
    """Take an exclusive flock on ``data/.lock`` or exit with a clear message.

    Two hamroh processes on one data dir would double-fire reminders
    and fight over the saved CC session, so refuse to boot. The flock
    dies with the process — including SIGKILL — so there is no stale-lock
    handling. The PID inside the file is informational for the operator.
    """
    lock_path = config.data_dir / ".lock"
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit(
            f"another hamroh instance is already running on {config.data_dir} "
            "(data/.lock is held). Stop it, or give this instance its own "
            "HAMROH_DATA_DIR."
        )
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


async def _replay_unconsumed(db: Database, engine: Engine) -> None:
    """Re-submit inbound messages buffered but never handed to CC.

    Runs once at boot, after the engine starts and before the dispatcher
    starts polling, so replayed messages can't interleave with fresh
    inbound. The debounce re-batches them into one turn.
    """
    messages = await fetch_unconsumed_inbound(db)
    if not messages:
        return
    log.info(
        "replaying %d message(s) buffered before the last shutdown",
        len(messages),
    )
    for m in messages:
        await engine.submit(m)


async def _open_db_and_stores(config: Config) -> tuple[Database, Plugins, _Stores]:
    """Open the database, load plugins, and warm every disk-backed store."""
    db = await Database.open(config.db_path)
    log.info("database ready at %s", config.db_path)

    project_root = Path(__file__).resolve().parent.parent
    plugins = load_plugins(project_root / "plugins.json")
    log.info(
        "plugins loaded: %d enabled mcp(s), %d disabled skill(s), "
        "%d disabled built-in tool(s), tool_groups=%s",
        len(plugins.mcps),
        len(plugins.skills_disabled),
        len(plugins.builtin_tools_disabled),
        dict(plugins.tool_groups),
    )

    stores = _build_stores(config, db, plugins)
    re_armed = await reset_stuck_reminders(db)
    if re_armed:
        log.info(
            "re-armed %d reminder(s) left mid-delivery by the last shutdown",
            re_armed,
        )
    await _seed_default_reminders(db, config)
    return db, plugins, stores


async def _start_mcp_server(
    app: _App,
    stores: _Stores,
    plugins: Plugins,
    chat_titles: dict[int, str],
) -> tuple[ToolContext, McpServer]:
    """Build the shared ToolContext and bring the MCP server live."""
    db = app.db

    async def db_logger(**kwargs):  # called by every MCP tool wrapper
        await insert_tool_call(db, ToolCall(**kwargs))

    browser_manager = BrowserManager(headless=app.config.browser_headless)
    ctx = ToolContext(
        bot=None,  # filled in once the dispatcher exists
        database=db,
        memory_store=stores.memory,
        instructions_store=stores.instructions,
        skills_store=stores.skills,
        attachment_store=stores.attachments,
        render_store=stores.renders,
        browser_manager=browser_manager,
        browser_session=BrowserSession(browser_manager),
        chat_titles=chat_titles,
    )
    mcp = McpServer(
        ctx,
        db_logger=db_logger,
        disabled=plugins.builtin_tools_disabled,
    )
    await mcp.start()
    log.info("mcp server live at %s", mcp.url)
    return ctx, mcp


def _build_cc_spec(
    config: Config, plugins: Plugins, mcp: McpServer, skills: SkillsStore
) -> CcSpawnSpec:
    """Write the schema + MCP config to a tmpdir and assemble the spawn spec.

    Tool-group toggles flow through ``plugins.json`` exclusively — edit
    the file and restart to flip. The skills index is rendered once here
    and baked into the system prompt, so adding/removing a skill takes
    effect on the next restart.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="hamroh-"))
    schema_path = tmpdir / "schema.json"
    schema_path.write_text(schema_json())
    extra_mcp, mcp_allowed_tools = _build_external_mcp_config(plugins)
    mcp_config_path = mcp.write_mcp_config(
        tmpdir / "mcp.json",
        extra_servers=extra_mcp,
    )
    log.info("mcp config written to %s", mcp_config_path)

    return CcSpawnSpec(
        binary=config.claude_code_bin,
        model=config.model,
        system_prompt_path=Path("prompts/system.md").resolve(),
        project_prompt_path=Path("prompts/project.md").resolve(),
        mcp_config_path=mcp_config_path,
        json_schema_path=schema_path,
        effort=config.effort,
        session_id=_load_session_id(config),
        cc_logs_dir=config.cc_logs_dir,
        enable_subagents=bool(plugins.tool_groups.get("subagents", False)),
        subagents_prompt_path=Path("prompts/subagents.md").resolve(),
        enable_bash=bool(plugins.tool_groups.get("bash", False)),
        enable_code=bool(plugins.tool_groups.get("code", False)),
        mcp_allowed_tools=tuple(mcp_allowed_tools),
        skills_index=render_skills_index(skills),
    )


def _make_on_cc_crash(app: _App):
    """Crash notifier: tell waiting chats (and always the owner) that CC
    is restarting."""

    async def _on_cc_crash(attempt: int, backoff: float) -> None:
        dispatcher = app.dispatcher
        if dispatcher is None:
            return
        engine = app.engine
        user_text = (
            f"⚠️ Technical issue, restarting "
            f"(attempt {attempt}, retrying in {backoff:.0f}s). "
            "Please resend your last message in a moment."
        )
        if engine is not None and engine._turn.active_chats:
            for chat_id in engine._turn.active_chats:
                try:
                    await dispatcher.bot.send_message(chat_id=chat_id, text=user_text)
                except Exception:
                    log.warning("crash notify to %s failed", chat_id, exc_info=True)
        owner_chat = app.config.owner_id
        if owner_chat not in (engine._turn.active_chats if engine else set()):
            try:
                await dispatcher.bot.send_message(
                    chat_id=owner_chat,
                    text=f"CC error (attempt {attempt}). Check logs.",
                )
            except Exception:
                log.warning("crash notify to owner failed", exc_info=True)

    return _on_cc_crash


def _make_on_cc_stale_session(app: _App):
    """Stale-session notifier: drop the persisted id and tell the owner."""

    async def _on_cc_stale_session(stale_id: str) -> None:
        dispatcher = app.dispatcher
        if dispatcher is None:
            return
        try:
            app.config.session_id_path.unlink(missing_ok=True)
        except OSError:
            log.exception("failed to delete stale session_id file")
        if app.engine is not None:
            await app.engine.stash_restore_context("stale-session")
        try:
            await dispatcher.bot.send_message(
                chat_id=app.config.owner_id,
                text=(
                    "ℹ️ Previous Claude Code session expired — "
                    "starting a fresh one. Your last message may need "
                    "to be resent. I'll carry a short recap of recent "
                    "messages into the next turn."
                ),
            )
        except Exception:
            log.warning("stale-session notify to owner failed", exc_info=True)

    return _on_cc_stale_session


def _make_on_cc_giveup(app: _App):
    """Give-up notifier: tell every waiting chat + the owner that CC is
    down for good and the operator must intervene."""

    async def _on_cc_giveup(crash_count: int) -> None:
        dispatcher = app.dispatcher
        if dispatcher is None:
            return
        user_text = (
            f"⚠️ Shutting down — Claude Code failed {crash_count} times. "
            "The operator needs to intervene."
        )
        chats_to_notify: set[int] = set()
        if app.engine is not None and app.engine._turn.active_chats:
            chats_to_notify.update(app.engine._turn.active_chats)
        chats_to_notify.add(app.config.owner_id)
        for chat_id in chats_to_notify:
            try:
                await dispatcher.bot.send_message(chat_id=chat_id, text=user_text)
            except Exception:
                log.warning("giveup notify to %s failed", chat_id, exc_info=True)

    return _on_cc_giveup


def _make_on_cc_status(app: _App):
    """Status heartbeat: while a turn keeps running, tell the waiting chats it's
    still working (every ``status_interval_seconds``) so a long task isn't
    silent. The turn keeps going — the owner can reply 'stop' to halt it."""

    async def _on_cc_status(elapsed: float, last_action: str | None) -> None:
        engine = app.engine
        dispatcher = app.dispatcher
        if engine is None or dispatcher is None or not engine._turn.active_chats:
            return
        minutes = max(1, int(elapsed // 60))
        step = f" (last step: {last_action})" if last_action else ""
        text = (
            f"⏳ Still working on this — about {minutes} min so far{step}. "
            "Reply 'stop' if you want me to halt."
        )
        for chat_id in set(engine._turn.active_chats):
            try:
                await dispatcher.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_to_message_id=engine._turn.reply_targets.get(chat_id),
                )
            except Exception:
                log.warning("status notify to %s failed", chat_id, exc_info=True)

    return _on_cc_status


def _build_dispatcher_and_engine(
    app: _App,
    stores: _Stores,
    chat_titles: dict[int, str],
) -> tuple[TelegramDispatcher, Engine]:
    """Construct the dispatcher (bot owner), then the engine wired to it."""
    assert app.worker is not None  # set before this runs; satisfies the type checker
    dispatcher = TelegramDispatcher(
        app.config,
        app.db,
        DispatcherDeps(chat_titles=chat_titles, rate_limiter=stores.rate_limiter),
    )
    engine = Engine(
        app.worker,
        app.config,
        EngineOptions(
            debounce_ms=app.config.debounce_ms,
            db=app.db,
            typing_action=_make_typing_action(dispatcher, app),
            error_notify=_make_error_notify(dispatcher),
        ),
    )
    return dispatcher, engine


#: Stable, non-zero draft identifier reused for every progress draft. Telegram
#: animates updates that share an id, which is exactly what we want — one live
#: draft per chat that morphs as the turn progresses.
PROGRESS_DRAFT_ID = 1


def _progress_draft_text(elapsed: float, last_action: str | None) -> str:
    """One-line progress shown in the live DM draft while a turn runs."""
    when = f"~{int(elapsed)}s" if elapsed < 60 else f"~{int(elapsed // 60)} min"
    step = f", last step: {last_action}" if last_action else ""
    return f"✍️ Working on it… ({when}{step})"


async def _send_progress_draft(
    dispatcher: TelegramDispatcher, app: _App, chat_id: int
) -> None:
    """Refresh the live "working…" draft for one DM (best-effort)."""
    worker = app.worker
    started = worker._turn_started_at if worker is not None else None
    elapsed = (time.monotonic() - started) if started else 0.0
    last_action = worker._last_tool_action if worker is not None else None
    text = _progress_draft_text(elapsed, last_action)
    try:
        await dispatcher.bot.send_message_draft(
            chat_id=chat_id, draft_id=PROGRESS_DRAFT_ID, text=text
        )
    except Exception as exc:
        log.warning("send_message_draft failed for chat %s: %s", chat_id, exc)


def _make_typing_action(dispatcher: TelegramDispatcher, app: _App) -> TypingAction:
    """Wire the engine's per-chat liveness signal.

    Normally fires ``bot.send_chat_action`` ("typing…"). When the progress-draft
    feature is on *and* the chat is a DM, fires ``bot.send_message_draft`` instead
    so a long job shows a live "working…" draft. Telegram only allows drafts in
    private chats, whose ids are positive (groups/supergroups are negative), so
    the sign of ``chat_id`` picks the right path without an extra API call.
    """

    async def _typing(chat_id: int) -> None:
        if app.config.progress_draft_enabled and chat_id > 0:
            await _send_progress_draft(dispatcher, app, chat_id)
            return
        try:
            ok = await dispatcher.bot.send_chat_action(chat_id=chat_id, action="typing")
            log.debug("send_chat_action chat=%s returned=%r", chat_id, ok)
        except Exception as exc:
            log.warning("send_chat_action failed for chat %s: %s", chat_id, exc)

    return _typing


def _make_error_notify(dispatcher: TelegramDispatcher) -> ErrorNotify:
    """Wire the engine's bypass error-notify to ``bot.send_message``."""

    async def _error_notify(chat_id: int, text: str) -> None:
        try:
            await dispatcher.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            log.warning("error notify failed for chat %s: %s", chat_id, exc)

    return _error_notify


async def _run_until_stopped(app: _App) -> None:
    """Block until SIGINT/SIGTERM, then tear down opposite to construction."""
    # All of these are wired during construction before this runs; the
    # asserts narrow the dataclass's ``| None`` fields for the type checker.
    assert app.worker is not None
    assert app.dispatcher is not None
    assert app.engine is not None
    assert app.mcp is not None
    assert app.reminder_task is not None
    stop_event = asyncio.Event()
    _install_signal_handlers(app.worker, stop_event)
    try:
        await stop_event.wait()
    finally:
        # Persist session id first so the next start resumes it.
        if app.worker.session_id:
            app.config.session_id_path.write_text(app.worker.session_id)
        app.reminder_task.cancel()
        await app.dispatcher.stop()
        await app.engine.stop()
        await app.worker.stop()
        await app.mcp.stop()
        if app.browser_session is not None:
            await app.browser_session.close()
        if app.browser_manager is not None:
            await app.browser_manager.close()
        await app.db.close()
        log.info("clean shutdown complete")
