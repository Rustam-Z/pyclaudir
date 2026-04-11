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
from pathlib import Path

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
    mcp_config_path = mcp.write_mcp_config(tmpdir / "mcp.json")
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
        session_id=session_id,
        cc_logs_dir=config.cc_logs_dir,
    )
    worker = CcWorker(spec, heartbeat=ctx.heartbeat)
    await worker.start()
    await worker.supervise()

    engine = Engine(worker, debounce_ms=config.debounce_ms, db=db)
    await engine.start()

    dispatcher = TelegramDispatcher(config, db, engine, chat_titles=chat_titles)
    ctx.bot = dispatcher.bot
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
