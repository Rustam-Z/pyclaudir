"""The engine — debouncer, queue, inject channel, control loop.

This is the heart of pyclaudir. The dispatcher calls :meth:`Engine.submit`
for every allowed inbound message. The engine batches them with a 1-second
debounce, formats them as XML the same way Claudir does, and ships them to
the CC worker. While CC is processing a turn, additional messages are
shovelled through the inject channel so they land in the same turn rather
than triggering a new one.

The engine itself owns the asyncio coordination — debounce timer, batch
buffer, processing flag, control loop. It does *not* own the CC worker's
lifecycle (the run loop in ``__main__`` does), nor the database
(persistence happens in the dispatcher before the engine ever sees a
message).
"""

from __future__ import annotations

import asyncio
import logging
import xml.sax.saxutils as sx
from datetime import datetime
from typing import TYPE_CHECKING

from .db.messages import fetch_reply_chain
from .models import ChatMessage

if TYPE_CHECKING:  # pragma: no cover
    from .cc_worker import CcWorker, TurnResult
    from .db.database import Database

log = logging.getLogger(__name__)

#: How many hops to walk back through a Telegram reply chain.
DEFAULT_REPLY_DEPTH = 3


def _attr(value: str) -> str:
    """XML attribute value escape that returns the inner string only."""
    return sx.quoteattr(value)[1:-1]


def _format_one(message: ChatMessage, parents_xml: str = "") -> str:
    ts = (
        message.timestamp.strftime("%H:%M")
        if isinstance(message.timestamp, datetime)
        else str(message.timestamp)
    )
    name = message.first_name or message.username or str(message.user_id)
    body = sx.escape(message.text)
    reply_attr = (
        f' reply_to="{message.reply_to_id}"' if message.reply_to_id is not None else ""
    )
    return (
        f'<msg id="{message.message_id}" chat="{message.chat_id}" '
        f'user="{message.user_id}" name="{_attr(name)}" time="{ts}"{reply_attr}>\n'
        f"{parents_xml}{body}\n</msg>"
    )


def format_messages_as_xml(messages: list[ChatMessage]) -> str:
    """Render a batch of messages as the Claudir-style ``<msg>`` XML.

    Pure / synchronous: no DB lookup, no reply-chain expansion. Used by
    tests and as a fallback when no database is wired.
    """
    return "\n".join(_format_one(m) for m in messages)


async def format_messages_with_context(
    messages: list[ChatMessage],
    db: "Database | None",
    *,
    max_depth: int = DEFAULT_REPLY_DEPTH,
) -> str:
    """Render a batch of messages with reply-chain context expanded.

    For every message in ``messages`` whose ``reply_to_id`` is set, walk our
    own ``messages`` table back up to ``max_depth`` hops and embed each
    parent inside the rendered ``<msg>`` block as ``<reply_chain><parent
    .../></reply_chain>``.

    Lookup misses fall back to the inline ``reply_to_text`` Telegram echoed
    in the original update (if present), so the model still sees something
    when our DB doesn't have the parent — e.g. the bot was just added to
    the group and the user immediately replied to a pre-existing message.

    If ``db`` is ``None`` we degrade to the pure formatter — same path as
    :func:`format_messages_as_xml`.
    """
    if db is None:
        return format_messages_as_xml(messages)

    rendered: list[str] = []
    for m in messages:
        parents_xml = ""
        if m.reply_to_id is not None:
            try:
                chain = await fetch_reply_chain(
                    db, m.chat_id, m.reply_to_id, max_depth=max_depth
                )
            except Exception:  # pragma: no cover
                log.exception("reply chain lookup failed for %s", m.message_id)
                chain = []

            if chain:
                parts: list[str] = ["<reply_chain>"]
                for p in chain:
                    pname = p["first_name"] or p["username"] or str(p["user_id"])
                    parts.append(
                        f'  <parent id="{p["message_id"]}" user="{p["user_id"]}" '
                        f'name="{_attr(pname)}" direction="{p["direction"]}" '
                        f'time="{p["timestamp"]}">'
                        f'{sx.escape(p["text"] or "")}'
                        f"</parent>"
                    )
                parts.append("</reply_chain>\n")
                parents_xml = "\n".join(parts)
            elif m.reply_to_text:
                # DB miss — fall back to whatever Telegram inlined.
                parents_xml = (
                    "<reply_chain>\n"
                    f'  <parent id="{m.reply_to_id}" source="telegram_inline">'
                    f"{sx.escape(m.reply_to_text)}</parent>\n"
                    "</reply_chain>\n"
                )
        rendered.append(_format_one(m, parents_xml))
    return "\n".join(rendered)


class Engine:
    def __init__(
        self,
        worker: "CcWorker",
        *,
        debounce_ms: int = 1000,
        db: "Database | None" = None,
    ) -> None:
        self._worker = worker
        self._debounce = debounce_ms / 1000.0
        self._db = db
        self._pending: list[ChatMessage] = []
        self._lock = asyncio.Lock()
        self._is_processing = asyncio.Event()
        self._debounce_task: asyncio.Task | None = None
        self._control_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._control_task = asyncio.create_task(
            self._control_loop(), name="pyclaudir-engine-loop"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        if self._control_task and not self._control_task.done():
            self._control_task.cancel()
            try:
                await self._control_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def submit(self, msg: ChatMessage) -> None:
        """Add an inbound message to the pending buffer.

        - If the engine is *not* currently processing a turn we (re)start the
          debounce timer; once it fires we drain the buffer and start a turn.
        - If the engine *is* processing a turn we still buffer here, but the
          control loop will drain whatever's in the buffer between turns. The
          inject path is used for *immediate* mid-turn delivery only when
          we're sure CC is mid-stream — see :meth:`_maybe_inject`.
        """
        async with self._lock:
            self._pending.append(msg)

        if self._is_processing.is_set():
            await self._maybe_inject()
            return

        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounce_then_kick())

    async def _debounce_then_kick(self) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        await self._kick()

    async def _kick(self) -> None:
        async with self._lock:
            if not self._pending or self._is_processing.is_set():
                return
            batch = self._pending
            self._pending = []
            self._is_processing.set()
        xml = await format_messages_with_context(batch, self._db)
        log.info("starting turn with %d msgs", len(batch))
        await self._worker.send(xml)

    async def _maybe_inject(self) -> None:
        """Flush any pending messages into the worker's inject queue."""
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
        xml = await format_messages_with_context(batch, self._db)
        await self._worker.inject(xml)
        log.info("injected %d msgs into running turn", len(batch))

    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    async def _control_loop(self) -> None:
        """Wait for each turn to finish and decide what to do next."""
        try:
            while not self._stop.is_set():
                if not self._is_processing.is_set():
                    # Idle. Wait for either work to arrive or a stop signal.
                    await asyncio.sleep(0.05)
                    continue

                result: TurnResult = await self._worker.wait_for_result()
                self._is_processing.clear()
                action = result.control.action if result.control else None
                log.info(
                    "turn done (action=%s, dropped_text=%s, text_blocks=%d)",
                    action, result.dropped_text, len(result.text_blocks),
                )

                if result.dropped_text:
                    # The model produced text but never called send_message.
                    # Tell it so on the next turn (Step 9 expands this).
                    error_xml = (
                        "<error>You produced text but did not call send_message. "
                        "Use the tool — text content blocks are invisible to the user.</error>"
                    )
                    await self._worker.send(error_xml)
                    self._is_processing.set()
                    continue

                if action == "sleep" and result.control and result.control.sleep_ms:
                    await asyncio.sleep(result.control.sleep_ms / 1000)

                # If new messages arrived while we were processing, kick them now.
                async with self._lock:
                    has_pending = bool(self._pending)
                if has_pending:
                    await self._kick()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            log.exception("engine control loop crashed")
