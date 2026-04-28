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
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .access import AccessConfig, load_access, save_access
from .cc_schema import schema_json
from .cc_worker import CcSpawnSpec, CcWorker
from .config import Config
from .plugins import load_plugins
from .db.database import Database
from .db.messages import insert_tool_call
from .db.reminders import (
    advance_recurring_reminder,
    fetch_due_reminders,
    insert_auto_seeded_reminder,
    mark_reminder_sent,
    pending_with_auto_seed_key,
)
from .engine import Engine
from .attachments_store import AttachmentStore
from .render_store import RenderStore
from .instructions_store import InstructionsStore
from .mcp_server import McpServer
from .memory_store import MemoryStore
from .rate_limiter import RateLimiter
from .skills_store import SkillsStore
from .telegram_io import TelegramDispatcher
from .tools.base import ToolContext

log = logging.getLogger("pyclaudir")


def _setup_logging() -> None:
    """Configure logging so the transcript is the star.

    The ``pyclaudir.tx`` logger emits one line per inbound/outbound/edit/
    delete/reaction message, prefixed ``[RX]`` / ``[TX]`` / etc. We quiet
    down the high-volume HTTP polling chatter so those lines stand out.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx prints one INFO line per long-poll getUpdates (every ~10s).
    # That spam buries the actual conversation. Silence everything below
    # WARNING for it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # MCP per-request logs are interesting when debugging tool calls but
    # noisy in normal operation. Comment this out if you want them back.
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)


_SELF_REFLECTION_KEY = "self-reflection-default"


async def _seed_default_reminders(db, config) -> None:
    """Ensure the default self-reflection reminder is active.

    The self-reflection loop is **mandatory** — the bot shouldn't be
    able to stop learning. On every startup we check whether a PENDING
    row with ``auto_seed_key='self-reflection-default'`` exists. If
    not (missing entirely, cancelled, deleted, whatever the reason),
    we re-seed. Cancellation is also blocked at the tool layer — see
    ``CancelReminderTool`` — so this is defense in depth against DB
    tampering or manual SQL.
    """
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
        auto_seed_key=_SELF_REFLECTION_KEY,
        chat_id=config.owner_id,
        user_id=-1,  # synthetic pseudo-user (same convention as reminder loop)
        text='<skill name="self-reflection">run</skill>',
        trigger_at=first_trigger.strftime("%Y-%m-%d %H:%M:%S"),
        cron_expr=cron_expr,
    )
    log.info(
        "seeded default self-reflection reminder (cron=%s, next=%s UTC)",
        cron_expr,
        first_trigger.strftime("%Y-%m-%d %H:%M:%S"),
    )


async def _async_main() -> None:
    _setup_logging()

    config = Config.from_env()
    config.ensure_dirs()

    # Bootstrap access.json on first run with the safest default:
    # owner-only DMs, no allowed chats. Owner adds others later via
    # /telegram:access (which mutates the file in place).
    if not config.access_path.exists():
        seed = AccessConfig(policy="owner_only", allowed_users=[], allowed_chats=[])
        save_access(config.access_path, seed)
        log.info("created %s (policy=owner_only, chats=[])", config.access_path)
    else:
        access = load_access(config.access_path)
        log.info(
            "access: policy=%s, allowed_users=%d, allowed_chats=%d",
            access.policy, len(access.allowed_users), len(access.allowed_chats),
        )

    db = await Database.open(config.db_path)
    log.info("database ready at %s", config.db_path)

    memory = MemoryStore(config.memories_dir)
    memory.ensure_root()
    rate_limiter = RateLimiter(
        db=db,
        limit=config.rate_limit_per_min,
        owner_id=config.owner_id,
    )
    project_root = Path(__file__).resolve().parent.parent
    instructions = InstructionsStore(
        project_md_path=project_root / "prompts" / "project.md",
        backup_dir=config.data_dir / "prompt_backups",
    )
    instructions.ensure_dirs()
    plugins = load_plugins(project_root / "plugins.json")
    log.info(
        "plugins loaded: %d enabled mcp(s), %d disabled skill(s), "
        "%d disabled built-in tool(s), tool_groups=%s",
        len(plugins.mcps),
        len(plugins.skills_disabled),
        len(plugins.builtin_tools_disabled),
        dict(plugins.tool_groups),
    )

    skills = SkillsStore(root=project_root / "skills", disabled=plugins.skills_disabled)
    skills.ensure_root()
    attachments = AttachmentStore(config.attachments_dir)
    renders = RenderStore(config.renders_dir)
    renders.ensure_root()

    # Seed the default self-reflection reminder if the operator hasn't
    # already seen one (even a cancelled row counts — we respect prior
    # decisions). Runs exactly once per persistent DB.
    await _seed_default_reminders(db, config)

    async def db_logger(**kwargs):  # called by every MCP tool wrapper
        await insert_tool_call(db, **kwargs)

    # Shared between dispatcher (writer) and outbound tools (reader).
    chat_titles: dict[int, str] = {}
    ctx = ToolContext(
        bot=None,  # filled in below once dispatcher exists
        database=db,
        memory_store=memory,
        instructions_store=instructions,
        skills_store=skills,
        attachment_store=attachments,
        render_store=renders,
        chat_titles=chat_titles,
    )

    mcp = McpServer(
        ctx,
        db_logger=db_logger,
        disabled=plugins.builtin_tools_disabled,
    )
    await mcp.start()
    log.info("mcp server live at %s", mcp.url)

    # Persist the schema and mcp config to temp files for the CC subprocess.
    tmpdir = Path(tempfile.mkdtemp(prefix="pyclaudir-"))
    schema_path = tmpdir / "schema.json"
    schema_path.write_text(schema_json())
    # External MCP servers alongside our local one. Each enabled entry
    # in ``plugins.json`` whose ``${VAR}`` references all resolved
    # contributes one stdio MCP server here. The plugin's ``name`` is
    # the dict key Claude Code uses to namespace tools as
    # ``mcp__<name>__<tool>`` — those names are load-bearing and visible
    # to the model.
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
                plugin.name, plugin.command,
            )
        else:  # http or sse — remote server, optional static auth headers
            entry: dict = {"type": plugin.type, "url": plugin.url}
            if plugin.headers:
                entry["headers"] = dict(plugin.headers)
            extra_mcp[plugin.name] = entry
            log.info(
                "mcp %s configured (type=%s, url=%s)",
                plugin.name, plugin.type, plugin.url,
            )
        mcp_allowed_tools.extend(plugin.allowed_tools)

    mcp_config_path = mcp.write_mcp_config(tmpdir / "mcp.json", extra_servers=extra_mcp)
    log.info("mcp config written to %s", mcp_config_path)

    # CC worker
    session_id = None
    if config.session_id_path.exists():
        session_id = config.session_id_path.read_text().strip() or None
        log.info("resuming cc session %s", session_id)

    # Tool-group toggles: ``plugins.json`` is the single source of
    # truth. Edit the file and restart to flip.
    enable_subagents = bool(plugins.tool_groups.get("subagents", False))
    enable_bash = bool(plugins.tool_groups.get("bash", False))
    enable_code = bool(plugins.tool_groups.get("code", False))

    spec = CcSpawnSpec(
        binary=config.claude_code_bin,
        model=config.model,
        system_prompt_path=Path("prompts/system.md").resolve(),
        project_prompt_path=Path("prompts/project.md").resolve(),
        mcp_config_path=mcp_config_path,
        json_schema_path=schema_path,
        effort=config.effort,
        session_id=session_id,
        cc_logs_dir=config.cc_logs_dir,
        enable_subagents=enable_subagents,
        subagents_prompt_path=Path("prompts/subagents.md").resolve(),
        enable_bash=enable_bash,
        enable_code=enable_code,
        mcp_allowed_tools=tuple(mcp_allowed_tools),
    )
    async def _on_cc_crash(attempt: int, backoff: float) -> None:
        user_text = (
            f"⚠️ Technical issue, restarting "
            f"(attempt {attempt}, retrying in {backoff:.0f}s). "
            "Please resend your last message in a moment."
        )
        if engine is not None and engine._active_chats:
            for chat_id in engine._active_chats:
                try:
                    await dispatcher.bot.send_message(chat_id=chat_id, text=user_text)
                except Exception:
                    log.warning("crash notify to %s failed", chat_id, exc_info=True)
        owner_chat = config.owner_id
        if owner_chat not in (engine._active_chats if engine else set()):
            try:
                await dispatcher.bot.send_message(
                    chat_id=owner_chat,
                    text=f"CC error (attempt {attempt}). Check logs.",
                )
            except Exception:
                log.warning("crash notify to owner failed", exc_info=True)

    async def _on_cc_giveup(crash_count: int) -> None:
        user_text = (
            f"⚠️ Shutting down — Claude Code failed {crash_count} times. "
            "The operator needs to intervene."
        )
        chats_to_notify: set[int] = set()
        if engine is not None and engine._active_chats:
            chats_to_notify.update(engine._active_chats)
        chats_to_notify.add(config.owner_id)
        for chat_id in chats_to_notify:
            try:
                await dispatcher.bot.send_message(chat_id=chat_id, text=user_text)
            except Exception:
                log.warning("giveup notify to %s failed", chat_id, exc_info=True)

    # Engine is declared here but constructed after dispatcher
    engine = None  # type: ignore[assignment]

    worker = CcWorker(
        spec, config,
        heartbeat=ctx.heartbeat,
        on_crash=_on_cc_crash, on_giveup=_on_cc_giveup,
    )
    await worker.start()
    await worker.supervise()

    # The dispatcher owns the bot, so we build it first, then hand a
    # closure into the engine for the typing indicator.
    dispatcher = TelegramDispatcher(  # type: ignore[arg-type]
        config,
        db,
        engine=None,
        chat_titles=chat_titles,
        rate_limiter=rate_limiter,
    )

    async def _typing(chat_id: int) -> None:
        t0 = time.monotonic()
        try:
            ok = await dispatcher.bot.send_chat_action(chat_id=chat_id, action="typing")
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.debug(
                "send_chat_action chat=%s returned=%r elapsed=%dms",
                chat_id, ok, elapsed_ms,
            )
        except Exception as exc:
            log.warning("send_chat_action failed for chat %s: %s", chat_id, exc)

    async def _error_notify(
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> None:
        try:
            kwargs: dict = {"chat_id": chat_id, "text": text}
            if reply_to_message_id:
                kwargs["reply_to_message_id"] = reply_to_message_id
            await dispatcher.bot.send_message(**kwargs)
        except Exception as exc:
            log.warning("error notify failed for chat %s: %s", chat_id, exc)

    engine = Engine(
        worker, config,
        debounce_ms=config.debounce_ms,
        db=db,
        typing_action=_typing,
        error_notify=_error_notify,
    )
    await engine.start()

    # Background reminder scheduler — polls every 60s for due reminders
    # and injects them into the engine as synthetic inbound messages.
    #
    # **Defer-when-busy policy**: a due reminder is held back if the
    # engine is mid-turn or a real user has been active within
    # ``REMINDER_QUIET_SECONDS`` (5 min). This stops long reminder
    # turns (most importantly the daily self-reflection skill) from
    # preempting active conversations. The reminder stays in the
    # ``pending`` set and is retried on the next 60s poll. To prevent
    # indefinite starvation in a continuously-busy deployment, a
    # reminder that's been overdue more than ``REMINDER_MAX_DEFER``
    # fires anyway.
    REMINDER_MAX_DEFER = 60 * 60  # 1 hour

    async def _reminder_loop() -> None:
        from .models import ChatMessage

        while True:
            await asyncio.sleep(60)
            try:
                now_dt = datetime.now(timezone.utc)
                now_utc = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                due = await fetch_due_reminders(db, now_utc)
                fired_count = 0
                for r in due:
                    # Defer if engine is busy or a user is active, unless
                    # this reminder is already too overdue to defer further.
                    try:
                        trigger_dt = datetime.strptime(
                            r["trigger_at"], "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc)
                        overdue_seconds = (now_dt - trigger_dt).total_seconds()
                    except (ValueError, TypeError):
                        overdue_seconds = 0.0  # malformed → fire now
                    if overdue_seconds < REMINDER_MAX_DEFER and engine.is_busy():
                        log.info(
                            "deferring reminder #%d (overdue %.0fs, engine busy)",
                            r["id"], overdue_seconds,
                        )
                        continue

                    reminder_xml = (
                        f'<reminder id="{r["id"]}" chat_id="{r["chat_id"]}" '
                        f'user_id="{r["user_id"]}">{r["text"]}</reminder>'
                    )
                    await engine.submit(ChatMessage(
                        chat_id=r["chat_id"],
                        message_id=0,
                        user_id=r["user_id"],
                        direction="in",
                        timestamp=datetime.now(timezone.utc),
                        text=reminder_xml,
                    ))
                    fired_count += 1
                    if r["cron_expr"]:
                        try:
                            from croniter import croniter

                            next_dt = croniter(
                                r["cron_expr"], datetime.now(timezone.utc)
                            ).get_next(datetime)
                            await advance_recurring_reminder(
                                db, r["id"],
                                next_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            )
                        except ImportError:
                            log.warning("croniter not installed, marking cron reminder #%d as sent", r["id"])
                            await mark_reminder_sent(db, r["id"])
                    else:
                        await mark_reminder_sent(db, r["id"])
                if fired_count:
                    log.info("fired %d reminder(s)", fired_count)
            except Exception:
                log.exception("reminder loop error")

    reminder_task = asyncio.create_task(_reminder_loop(), name="pyclaudir-reminders")

    dispatcher.engine = engine
    ctx.bot = dispatcher.bot
    # Wire send_message → engine notification so the typing indicator
    # stops the moment the user has the message in their hand, not when
    # the entire CC turn officially ends.
    ctx.on_chat_replied = engine.notify_chat_replied
    await dispatcher.start()
    log.info("pyclaudir is live")

    stop_event = asyncio.Event()

    def _stop(*_a):
        log.info("signal received, shutting down")
        # Tell the cc supervisor we're shutting down BEFORE it observes
        # the subprocess exit (the SIGINT propagates to the same process
        # group, so cc is exiting in parallel). Without this the
        # supervisor treats the clean exit as a crash and respawns.
        worker._stop_supervisor.set()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    try:
        await stop_event.wait()
    finally:
        # Persist the final session id so a restart can resume.
        if worker.session_id:
            config.session_id_path.write_text(worker.session_id)
        reminder_task.cancel()
        await dispatcher.stop()
        await engine.stop()
        await worker.stop()
        await mcp.stop()
        await db.close()
        log.info("clean shutdown complete")


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
