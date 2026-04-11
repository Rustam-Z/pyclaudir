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
from typing import TYPE_CHECKING, Awaitable, Callable

from .db.messages import fetch_reply_chain
from .models import ChatMessage

#: Telegram's typing action expires after ~5s on the server side, so we
#: refresh slightly faster than that to avoid a visible gap.
TYPING_REFRESH_SECONDS = 4.0

#: Telegram clients suppress very brief typing displays to avoid flicker —
#: typing that's "live" for less than ~1 second often never visually
#: renders in the user's client. We enforce a minimum visible duration
#: from the moment the first typing call fires, so that even when the
#: model responds in a fraction of a second the user actually sees the
#: indicator. Concretely: ``notify_chat_replied`` defers the actual
#: dismissal until this many seconds have elapsed since typing started.
MIN_TYPING_VISIBLE_SECONDS = 1.5

#: Async callable shape: ``await typing_action(chat_id)`` should fire one
#: ``send_chat_action`` to that chat. Engine doesn't import telegram.
TypingAction = Callable[[int], Awaitable[None]]

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
        typing_action: TypingAction | None = None,
    ) -> None:
        self._worker = worker
        self._debounce = debounce_ms / 1000.0
        self._db = db
        #: Optional callback that shows the "typing..." indicator in a
        #: Telegram chat. Wired by ``__main__.py`` to ``bot.send_chat_action``.
        self._typing_action = typing_action
        self._pending: list[ChatMessage] = []
        self._lock = asyncio.Lock()
        self._is_processing = asyncio.Event()
        self._debounce_task: asyncio.Task | None = None
        self._control_task: asyncio.Task | None = None
        self._typing_task: asyncio.Task | None = None
        self._typing_chats: set[int] = set()
        #: Set whenever something changes the typing set (a chat is added
        #: or removed). The typing loop ``wait_for``s this with a 4-second
        #: timeout, so it wakes immediately on a removal instead of sleeping
        #: out the full refresh interval.
        self._typing_wake = asyncio.Event()
        #: ``time.monotonic()`` value at the moment typing was first armed
        #: for the current turn. Used by :meth:`notify_chat_replied` to
        #: enforce :data:`MIN_TYPING_VISIBLE_SECONDS`.
        self._typing_started_at: float = 0.0
        #: Background task that defers the actual stop when ``notify_chat_replied``
        #: fires before the minimum visible duration has elapsed.
        self._typing_deferred_stop: asyncio.Task | None = None
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
        await self._stop_typing()

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
        # Show "typing..." in every chat involved in this batch.
        await self._start_typing({m.chat_id for m in batch})
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

        # Re-arm the typing indicator for the injected chats. There are
        # two cases to handle, and the bug we're fixing was that we only
        # handled the first one:
        #
        # 1. Typing loop is still running (i.e., the model hasn't sent
        #    anything yet this turn). Just add the new chats to the set
        #    so the next refresh tick covers them.
        #
        # 2. Typing loop has already exited because ``notify_chat_replied``
        #    fired earlier this turn (the model sent its first reply, we
        #    stopped typing, and now the user is firing a follow-up while
        #    CC is still processing the wrap-up of the previous turn —
        #    StructuredOutput etc.). In this case the loop is gone and we
        #    must restart it from scratch — same path as a fresh turn.
        new_chats = {m.chat_id for m in batch}
        if self._typing_task is not None and not self._typing_task.done():
            self._typing_chats.update(new_chats)
        else:
            await self._start_typing(new_chats)

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def _start_typing(self, chat_ids: set[int]) -> None:
        """Fire the first typing call synchronously, then spawn the refresh loop."""
        log.info(
            "start_typing called: chats=%s action_set=%s task_state=%s",
            chat_ids,
            self._typing_action is not None,
            "None" if self._typing_task is None else (
                "done" if self._typing_task.done() else "running"
            ),
        )
        if self._typing_action is None or not chat_ids:
            return
        import time

        self._typing_chats = set(chat_ids)
        self._typing_wake.clear()
        self._typing_started_at = time.monotonic()
        await self._fire_typing_once()
        if self._typing_task is None or self._typing_task.done():
            self._typing_task = asyncio.create_task(
                self._typing_refresh_loop(), name="pyclaudir-typing"
            )

    def notify_chat_replied(self, chat_id: int) -> None:
        """Called by ``send_message`` the moment Telegram confirms delivery.

        Drops the chat from the typing set and wakes the loop so it exits.
        But — and this is the subtle part — if the typing indicator has
        been "live" for less than :data:`MIN_TYPING_VISIBLE_SECONDS`, we
        defer the actual stop. This is because Telegram clients suppress
        very brief typing displays to avoid flicker, so a fast turn 2
        (warm CC, ~1s response) was reaching ``notify_chat_replied``
        before the indicator had a chance to render. The user observed
        "typing only shows on the first message after start" because the
        first message was naturally slow (cold cache), and subsequent
        messages were too fast for typing to render at all.

        This is a sync function (not async) because it's called from
        inside the ``send_message`` tool's coroutine and we don't want
        to introduce an extra ``await`` between message delivery and
        notification.
        """
        if chat_id not in self._typing_chats:
            return

        import time

        elapsed = time.monotonic() - self._typing_started_at
        remaining = MIN_TYPING_VISIBLE_SECONDS - elapsed

        if remaining <= 0:
            # Typing has been live long enough; stop immediately.
            self._typing_chats.discard(chat_id)
            self._typing_wake.set()
            return

        # Too fast — defer the discard so the indicator is visible for
        # at least MIN_TYPING_VISIBLE_SECONDS from when it started.
        # During the deferral the typing loop keeps refreshing.
        async def _deferred_discard() -> None:
            try:
                await asyncio.sleep(remaining)
            except asyncio.CancelledError:
                return
            self._typing_chats.discard(chat_id)
            self._typing_wake.set()

        # Schedule it; we don't await — notify_chat_replied returns
        # immediately so the send_message tool isn't blocked.
        self._typing_deferred_stop = asyncio.create_task(
            _deferred_discard(), name="pyclaudir-typing-deferred-stop"
        )

    async def _stop_typing(self) -> None:
        self._typing_chats.clear()
        self._typing_wake.set()
        # Cancel any pending deferred discard so it doesn't fire after we
        # already stopped.
        if self._typing_deferred_stop is not None and not self._typing_deferred_stop.done():
            self._typing_deferred_stop.cancel()
            try:
                await self._typing_deferred_stop
            except (asyncio.CancelledError, Exception):
                pass
        self._typing_deferred_stop = None
        if self._typing_task is not None and not self._typing_task.done():
            self._typing_task.cancel()
            try:
                await self._typing_task
            except (asyncio.CancelledError, Exception):
                pass
        self._typing_task = None

    async def _fire_typing_once(self) -> None:
        if self._typing_action is None:
            return
        for chat_id in list(self._typing_chats):
            try:
                await self._typing_action(chat_id)
                log.info("typing fired for chat %s", chat_id)
            except Exception as exc:  # pragma: no cover
                log.warning("typing action failed for chat %s: %s", chat_id, exc)

    async def _typing_refresh_loop(self) -> None:
        """Refresh typing every ~4s (the first call already fired in start).

        Telegram's typing action expires server-side after ~5s, so we
        refresh slightly faster than that to avoid a visible gap. The
        first call has already been awaited synchronously by
        :meth:`_start_typing`, so this loop only handles the *subsequent*
        ticks.

        Between refreshes we ``wait_for`` the wake event with a 4s timeout
        so :meth:`notify_chat_replied` can short-circuit the sleep and exit
        the loop immediately when the model successfully sends a message.
        """
        try:
            while self._typing_chats:
                self._typing_wake.clear()
                try:
                    await asyncio.wait_for(
                        self._typing_wake.wait(),
                        timeout=TYPING_REFRESH_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                if not self._typing_chats:
                    return
                await self._fire_typing_once()
        except asyncio.CancelledError:
            raise

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
                await self._stop_typing()
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
                    # Re-show typing for the same chats — they're still
                    # waiting for an actual reply.
                    if self._typing_chats:
                        await self._start_typing(set(self._typing_chats))
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
