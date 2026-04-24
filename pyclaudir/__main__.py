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
import os
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


#: Cron expression for the daily self-reflection reminder.
#: 17:00 UTC = 22:00 in Asia/Tashkent (UTC+5). Change the ENV var
#: ``PYCLAUDIR_SELF_REFLECTION_CRON`` to override without editing code.
_DEFAULT_SELF_REFLECTION_CRON = "0 17 * * *"
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

    cron_expr = os.environ.get(
        "PYCLAUDIR_SELF_REFLECTION_CRON", _DEFAULT_SELF_REFLECTION_CRON
    )
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

    # Bootstrap access.json from env vars on first run.
    if not config.access_path.exists():
        seed_chats = []
        raw = os.environ.get("PYCLAUDIR_ALLOWED_CHATS", "")
        if raw:
            seed_chats = [int(c.strip()) for c in raw.split(",") if c.strip()]
        seed = AccessConfig(
            dm_policy="owner_only",
            allowed_users=[],
            allowed_chats=seed_chats,
        )
        save_access(config.access_path, seed)
        log.info("created %s (dm_policy=owner_only, chats=%s)", config.access_path, seed_chats)
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
        project_root=project_root,
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
        owner_id=config.owner_id,
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
    jira_url = os.environ.get("JIRA_URL", "")
    jira_username = os.environ.get("JIRA_USERNAME", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    extra_mcp: dict = {}
    if jira_url and jira_username and jira_token:
        extra_mcp["mcp-atlassian"] = {
            "type": "stdio",
            "command": "mcp-atlassian",
            "args": [],
            "env": {
                "JIRA_URL": jira_url,
                "JIRA_USERNAME": jira_username,
                "JIRA_API_TOKEN": jira_token,
            },
        }
        log.info("mcp-atlassian configured (jira=%s, user=%s)", jira_url, jira_username)
    else:
        log.info("mcp-atlassian skipped (JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN not set)")

    # GitLab via @zereight/mcp-gitlab (read-only + MR comments).
    gitlab_url = os.environ.get("GITLAB_URL", "")
    gitlab_token = os.environ.get("GITLAB_TOKEN", "")
    if gitlab_url and gitlab_token:
        extra_mcp["mcp-gitlab"] = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@zereight/mcp-gitlab"],
            "env": {
                "GITLAB_PERSONAL_ACCESS_TOKEN": gitlab_token,
                "GITLAB_API_URL": gitlab_url.rstrip("/") + "/api/v4",
            },
        }
        log.info("mcp-gitlab configured (url=%s)", gitlab_url)
    else:
        log.info("mcp-gitlab skipped (GITLAB_URL / GITLAB_TOKEN not set)")

    mcp_config_path = mcp.write_mcp_config(tmpdir / "mcp.json", extra_servers=extra_mcp)
    log.info("mcp config written to %s", mcp_config_path)

    # CC worker
    session_id = None
    if config.session_id_path.exists():
        session_id = config.session_id_path.read_text().strip() or None
        log.info("resuming cc session %s", session_id)

    spec = CcSpawnSpec(
        binary=config.claude_code_bin,
        model=config.model,
        system_prompt_path=Path("prompts/system.md").resolve(),
        project_prompt_path=Path(
            os.environ.get("PYCLAUDIR_PROJECT_PROMPT", "prompts/project.md")
        ).resolve(),
        mcp_config_path=mcp_config_path,
        json_schema_path=schema_path,
        effort=config.effort,
        session_id=session_id,
        cc_logs_dir=config.cc_logs_dir,
    )
    async def _on_cc_crash(stderr_tail: list[str], attempt: int, backoff: float) -> None:
        # Notify active chats if the engine has any
        if engine is not None and engine._active_chats:
            for chat_id in engine._active_chats:
                try:
                    await dispatcher.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ I ran into a technical issue and need to restart "
                            f"(attempt {attempt}, retrying in {backoff:.0f}s). "
                            f"Please resend your last message in a moment."
                        ),
                    )
                except Exception:
                    pass
        # Also notify owner if it's a different chat
        owner_chat = config.owner_id
        if owner_chat not in (engine._active_chats if engine else set()):
            try:
                stderr_summary = "\n".join(stderr_tail[-3:]) if stderr_tail else "(no stderr)"
                await dispatcher.bot.send_message(
                    chat_id=owner_chat,
                    text=(
                        f"🔧 CC subprocess crashed (attempt {attempt}, "
                        f"backoff {backoff:.0f}s).\n\n"
                        f"Last stderr:\n```\n{stderr_summary}\n```"
                    ),
                    parse_mode="MarkdownV2" if "`" not in stderr_summary else None,
                )
            except Exception:
                pass

    # Engine is declared here but constructed after dispatcher
    engine = None  # type: ignore[assignment]

    worker = CcWorker(spec, heartbeat=ctx.heartbeat, on_crash=_on_cc_crash)
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
        tool_ctx=ctx,
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
        worker,
        debounce_ms=config.debounce_ms,
        db=db,
        typing_action=_typing,
        error_notify=_error_notify,
    )
    await engine.start()

    # Background reminder scheduler — polls every 60s for due reminders
    # and injects them into the engine as synthetic inbound messages.
    async def _reminder_loop() -> None:
        from .models import ChatMessage

        while True:
            await asyncio.sleep(60)
            try:
                now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                due = await fetch_due_reminders(db, now_utc)
                for r in due:
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
                if due:
                    log.info("fired %d reminder(s)", len(due))
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
