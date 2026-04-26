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
from datetime import datetime, timezone
from pathlib import Path

from .access import AccessConfig, load_access, save_access
from .cc_schema import schema_json
from .cc_worker import CcSpawnSpec, CcWorker
from .config import Config
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
        seed = AccessConfig(dm_policy="owner_only", allowed_users=[], allowed_chats=[])
        save_access(config.access_path, seed)
        log.info("created %s (dm_policy=owner_only, chats=[])", config.access_path)
    else:
        access = load_access(config.access_path)
        log.info(
            "access: dm_policy=%s, allowed_users=%d, allowed_chats=%d",
            access.dm_policy, len(access.allowed_users), len(access.allowed_chats),
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
    skills = SkillsStore(root=project_root / "skills")
    skills.ensure_root()

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
        chat_titles=chat_titles,
    )

    mcp = McpServer(ctx, db_logger=db_logger)
    await mcp.start()
    log.info("mcp server live at %s", mcp.url)

    # Persist the schema and mcp config to temp files for the CC subprocess.
    tmpdir = Path(tempfile.mkdtemp(prefix="pyclaudir-"))
    schema_path = tmpdir / "schema.json"
    schema_path.write_text(schema_json())
    # External MCP servers alongside our local one. The community
    # mcp-atlassian server (sooperset/mcp-atlassian) runs locally via
    # stdio and talks directly to the Jira REST API.
    extra_mcp: dict = {}
    if config.jira_url and config.jira_username and config.jira_api_token:
        extra_mcp["mcp-atlassian"] = {
            "type": "stdio",
            "command": "mcp-atlassian",
            "args": [],
            "env": {
                "JIRA_URL": config.jira_url,
                "JIRA_USERNAME": config.jira_username,
                "JIRA_API_TOKEN": config.jira_api_token,
            },
        }
        log.info(
            "mcp-atlassian configured (jira=%s, user=%s)",
            config.jira_url, config.jira_username,
        )
    else:
        log.info("mcp-atlassian skipped (JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN not set)")

    # GitLab via @zereight/mcp-gitlab (read-only + MR comments).
    if config.gitlab_url and config.gitlab_token:
        extra_mcp["mcp-gitlab"] = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@zereight/mcp-gitlab"],
            "env": {
                "GITLAB_PERSONAL_ACCESS_TOKEN": config.gitlab_token,
                "GITLAB_API_URL": config.gitlab_url.rstrip("/") + "/api/v4",
            },
        }
        log.info("mcp-gitlab configured (url=%s)", config.gitlab_url)
    else:
        log.info("mcp-gitlab skipped (GITLAB_URL / GITLAB_TOKEN not set)")

    # GitHub via @modelcontextprotocol/server-github. Token-only by
    # default (MCP server talks to github.com); set GITHUB_HOST in the
    # operator's env for GitHub Enterprise.
    if config.github_token:
        github_env: dict[str, str] = {
            "GITHUB_PERSONAL_ACCESS_TOKEN": config.github_token,
        }
        if config.github_host:
            github_env["GITHUB_HOST"] = config.github_host
        extra_mcp["github"] = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": github_env,
        }
        log.info(
            "mcp-github configured (host=%s)",
            config.github_host or "github.com",
        )
    else:
        log.info("mcp-github skipped (GITHUB_PERSONAL_ACCESS_TOKEN not set)")

    mcp_config_path = mcp.write_mcp_config(tmpdir / "mcp.json", extra_servers=extra_mcp)
    log.info("mcp config written to %s", mcp_config_path)

    # CC worker
    session_id = None
    if config.session_id_path.exists():
        session_id = config.session_id_path.read_text().strip() or None
        log.info("resuming cc session %s", session_id)

    # Integration tool surfaces are derived from credential presence —
    # the same predicates used above to decide whether to spawn the MCP
    # server. Single source of truth: env-set creds → server runs AND
    # tools are advertised in the allowlist.
    enable_jira = bool(
        config.jira_url and config.jira_username and config.jira_api_token
    )
    enable_gitlab = bool(config.gitlab_url and config.gitlab_token)
    enable_github = bool(config.github_token)

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
        enable_subagents=config.enable_subagents,
        subagents_prompt_path=Path("prompts/subagents.md").resolve(),
        enable_bash=config.enable_bash,
        enable_code=config.enable_code,
        enable_jira=enable_jira,
        enable_gitlab=enable_gitlab,
        enable_github=enable_github,
    )
    from .cc_failure_classifier import classify_cc_failure

    async def _on_cc_crash(stderr_tail: list[str], attempt: int, backoff: float) -> None:
        # Try to classify the crash cause from stderr. If we recognise it
        # (auth failure, model-access, quota), the user gets a targeted
        # message instead of the generic "technical issue" line.
        classification = classify_cc_failure(stderr_tail)
        if classification is not None:
            user_text = (
                f"{classification.user_message}\n\n"
                f"(attempt {attempt}, retrying in {backoff:.0f}s)"
            )
        else:
            user_text = (
                f"⚠️ I ran into a technical issue and need to restart "
                f"(attempt {attempt}, retrying in {backoff:.0f}s). "
                f"Please resend your last message in a moment."
            )

        if engine is not None and engine._active_chats:
            for chat_id in engine._active_chats:
                try:
                    await dispatcher.bot.send_message(chat_id=chat_id, text=user_text)
                except Exception:
                    log.warning("crash notify to %s failed", chat_id, exc_info=True)
        # Also notify owner if it's a different chat, with the raw
        # stderr tail so they can debug.
        owner_chat = config.owner_id
        if owner_chat not in (engine._active_chats if engine else set()):
            try:
                stderr_summary = "\n".join(stderr_tail[-3:]) if stderr_tail else "(no stderr)"
                await dispatcher.bot.send_message(
                    chat_id=owner_chat,
                    text=(
                        f"🔧 CC subprocess crashed "
                        f"(attempt {attempt}, backoff {backoff:.0f}s, "
                        f"kind={classification.kind if classification else 'unknown'}).\n\n"
                        f"Last stderr:\n{stderr_summary}"
                    ),
                )
            except Exception:
                log.warning("crash notify to owner failed", exc_info=True)

    async def _on_cc_giveup(stderr_tail: list[str], crash_count: int) -> None:
        """Terminal: the crash budget is exhausted. Tell every waiting
        chat and the owner that the bot is down and manual intervention
        is needed. Best-effort — if Telegram itself is down we just log.
        """
        classification = classify_cc_failure(stderr_tail)
        terminal_line = (
            "⚠️ I'm shutting down — Claude Code has failed to start "
            f"{crash_count} times in a row and I've given up retrying. "
            "The operator needs to intervene."
        )
        if classification is not None:
            user_text = f"{classification.user_message}\n\n{terminal_line}"
        else:
            user_text = terminal_line

        chats_to_notify: set[int] = set()
        if engine is not None and engine._active_chats:
            chats_to_notify.update(engine._active_chats)
        chats_to_notify.add(config.owner_id)

        for chat_id in chats_to_notify:
            try:
                await dispatcher.bot.send_message(chat_id=chat_id, text=user_text)
            except Exception:
                log.warning("giveup notify to %s failed", chat_id, exc_info=True)

        # Also ship the stderr tail to the owner so they can diagnose.
        try:
            stderr_summary = "\n".join(stderr_tail[-5:]) if stderr_tail else "(no stderr)"
            await dispatcher.bot.send_message(
                chat_id=config.owner_id,
                text=(
                    f"🔥 CC crash-loop terminal "
                    f"(count={crash_count}, "
                    f"kind={classification.kind if classification else 'unknown'}).\n\n"
                    f"Last stderr:\n{stderr_summary}"
                ),
            )
        except Exception:
            log.warning("giveup stderr notify to owner failed", exc_info=True)

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
        import time as _t

        t0 = _t.monotonic()
        try:
            ok = await dispatcher.bot.send_chat_action(chat_id=chat_id, action="typing")
            elapsed_ms = int((_t.monotonic() - t0) * 1000)
            # Temporarily INFO so we can confirm PTB is actually accepting
            # the call on turn 2 and beyond. Drop back to DEBUG once the
            # typing visibility issue is conclusively diagnosed.
            log.info(
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
