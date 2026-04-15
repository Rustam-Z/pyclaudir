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
from pathlib import Path

from .access import AccessConfig, load_access, save_access
from .cc_schema import schema_json
from .cc_worker import CcSpawnSpec, CcWorker
from .config import Config
from .db.database import Database
from .db.messages import insert_tool_call
from .engine import Engine
from .mcp_server import McpServer
from .memory_store import MemoryStore
from .rate_limiter import RateLimiter
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
    rate_limiter = RateLimiter(limit=config.rate_limit_per_min)

    async def db_logger(**kwargs):  # called by every MCP tool wrapper
        await insert_tool_call(db, **kwargs)

    # Shared between dispatcher (writer) and outbound tools (reader).
    chat_titles: dict[int, str] = {}
    ctx = ToolContext(
        bot=None,  # filled in below once dispatcher exists
        database=db,
        memory_store=memory,
        rate_limiter=rate_limiter,
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
    jira_url = os.environ.get("JIRA_URL", "")
    jira_username = os.environ.get("JIRA_USERNAME", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    extra_mcp: dict = {}
    if jira_url and jira_username and jira_token:
        extra_mcp["mcp-atlassian"] = {
            "type": "stdio",
            "command": "uvx",
            "args": ["mcp-atlassian"],
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
    dispatcher = TelegramDispatcher(config, db, engine=None, chat_titles=chat_titles)  # type: ignore[arg-type]

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

    async def _error_notify(chat_id: int, text: str) -> None:
        try:
            await dispatcher.bot.send_message(chat_id=chat_id, text=text)
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
    dispatcher.engine = engine
    ctx.bot = dispatcher.bot
    # Wire send_message → engine notification so the typing indicator
    # stops the moment the user has the message in their hand, not when
    # the entire CC turn officially ends.
    ctx.on_chat_replied = engine.notify_chat_replied
    await dispatcher.start()
    log.info("nodira is live")

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
