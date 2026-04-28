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
from typing import TYPE_CHECKING, Awaitable, Callable

from ..cc_failure_classifier import CcFailureClassification, classify_cc_failure
from ..config import Config
from ..models import ChatMessage
from .format import format_messages_with_context

#: How often we re-fire ``send_chat_action`` while a turn is in flight.
#: Telegram's typing action expires after ~5s on the server side; matching
#: that interval keeps the indicator continuous without spamming the API.
TYPING_REFRESH_SECONDS = 5

# Failure-handling thresholds — progress-notify window and the dropped-
# text retry cap — live on ``Config`` (see ``progress_notify_seconds`` /
# ``tool_error_max_count``). The dropped-text cap reuses the tool-error
# breaker threshold so operators tune one knob, not two.

#: Telegram clients suppress very brief typing displays to avoid flicker —
#: typing that's "live" for less than ~1 second often never visually
#: renders in the user's client. We enforce a minimum visible duration
#: from the moment the first typing call fires, so that even when the
#: model responds in a fraction of a second the user actually sees the
#: indicator. Concretely: ``notify_chat_replied`` defers the actual
#: dismissal until this many seconds have elapsed since typing started.
MIN_TYPING_VISIBLE_SECONDS = 1

#: How long after the last real user message the engine still considers
#: itself "busy" for the purpose of firing reminders. Used by
#: :meth:`Engine.is_busy`. The reminder loop checks this to defer
#: firing (most importantly the daily self-reflection skill) so it
#: doesn't preempt active conversations — the user types, the
#: reminder loop sees recent activity, and pushes the fire by one
#: poll cycle (60s) until things go quiet.
REMINDER_QUIET_SECONDS = 5 * 60

#: Async callable shape: ``await typing_action(chat_id)`` should fire one
#: ``send_chat_action`` to that chat. Engine doesn't import telegram.
TypingAction = Callable[[int], Awaitable[None]]

#: Async callable shape:
#: ``await error_notify(chat_id, text, reply_to_message_id=None)``
#: sends a message directly via the bot, bypassing the MCP layer
#: (which is dead when we need this). When ``reply_to_message_id``
#: is set the bot replies to that message (used by the progress
#: watchdog so the "still working" notice threads to the user's
#: request and routes to the correct chat by construction).
#: Engine doesn't import telegram.
ErrorNotify = Callable[[int, str, "int | None"], Awaitable[None]]

if TYPE_CHECKING:  # pragma: no cover
    from ..cc_worker import CcWorker, TurnResult
    from ..db.database import Database

log = logging.getLogger("pyclaudir.engine")


class Engine:
    def __init__(
        self,
        worker: "CcWorker",
        config: Config,
        *,
        debounce_ms: int = 1000,
        db: "Database | None" = None,
        typing_action: TypingAction | None = None,
        error_notify: ErrorNotify | None = None,
    ) -> None:
        self._worker = worker
        self._debounce = debounce_ms / 1000.0
        self._db = db
        # Cache hot-path knobs so the control loop and dropped-text
        # handler don't dereference Config on every event. Tests can
        # override (``eng._progress_notify_seconds = 0.05``) without
        # rebuilding the Config.
        self._tool_error_max_count: int = config.tool_error_max_count
        self._progress_notify_seconds: float = config.progress_notify_seconds
        #: Optional callback that shows the "typing..." indicator in a
        #: Telegram chat. Wired by ``__main__.py`` to ``bot.send_chat_action``.
        self._typing_action = typing_action
        #: Optional callback to send error messages directly via the bot
        #: when CC is down and the MCP path is unavailable.
        self._error_notify = error_notify
        #: Chat IDs from the most recent batch whose users are actually
        #: waiting on a reply. Synthetic reminders (``message_id == 0``)
        #: are excluded — same filter as ``_active_triggers`` below — so
        #: the progress watchdog and turn-start typing indicator skip
        #: reminder-only turns where there is no human to notify.
        self._active_chats: set[int] = set()
        #: For each active chat, the most recent inbound ``message_id``
        #: in the current turn's batch. The progress watchdog uses this
        #: as ``reply_to_message_id`` so the "still working" notice
        #: threads to the user's own message and is guaranteed to land
        #: in the correct chat. Synthetic messages (reminders,
        #: ``message_id == 0``) are excluded — Telegram would reject a
        #: reply to a non-existent message.
        self._active_triggers: dict[int, int] = {}
        self._pending: list[ChatMessage] = []
        self._lock = asyncio.Lock()
        self._is_processing = asyncio.Event()
        self._debounce_task: asyncio.Task | None = None
        self._control_task: asyncio.Task | None = None
        self._typing_task: asyncio.Task | None = None
        self._typing_chats: set[int] = set()
        #: Set whenever something changes the typing set (a chat is added
        #: or removed). The typing loop ``wait_for``s this with a
        #: ``TYPING_REFRESH_SECONDS`` timeout, so it wakes immediately on a
        #: removal instead of sleeping out the full refresh interval.
        self._typing_wake = asyncio.Event()
        #: ``time.monotonic()`` value at the moment typing was first armed
        #: for the current turn. Used by :meth:`notify_chat_replied` to
        #: enforce :data:`MIN_TYPING_VISIBLE_SECONDS`.
        self._typing_started_at: float = 0.0
        #: Background task that defers the actual stop when ``notify_chat_replied``
        #: fires before the minimum visible duration has elapsed.
        self._typing_deferred_stop: asyncio.Task | None = None
        #: Chats that have received at least one ``send_message`` reply
        #: during the current turn. Populated by ``notify_chat_replied``,
        #: cleared on each new turn in ``_kick``. The progress watchdog
        #: skips these chats — no point telling the user "still working"
        #: when they've already seen the model's first reply.
        self._replied_chats_this_turn: set[int] = set()
        #: Count of consecutive ``dropped_text`` results across turns.
        #: Reset on (a) a new user turn via ``_kick``, (b) any successful
        #: turn, and (c) after the cap is hit and the user has been
        #: notified — so their next follow-up message isn't pre-tainted
        #: by a prior failure. Bounded by ``Config.tool_error_max_count``
        #: — same knob the tool-error circuit breaker uses.
        self._dropped_text_retries: int = 0
        #: ``time.monotonic()`` of the last real user inbound (mid > 0).
        #: 0.0 means "no user has ever messaged this process". Used by
        #: :meth:`is_busy` to defer reminder firing during active
        #: conversations.
        self._last_user_inbound_at: float = 0.0
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
        if msg.message_id > 0:
            import time as _t
            self._last_user_inbound_at = _t.monotonic()
        async with self._lock:
            self._pending.append(msg)

        if self._is_processing.is_set():
            await self._maybe_inject()
            return

        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounce_then_kick())

    def is_busy(self) -> bool:
        """True if the engine is mid-turn or a real user has been active
        within the last :data:`REMINDER_QUIET_SECONDS`.

        The reminder loop checks this before firing each due reminder so
        long reminder turns (e.g. self-reflection) don't preempt
        ongoing user conversations. A reminder that's "too overdue" can
        bypass this — see the loop in ``__main__._reminder_loop``.
        """
        import time as _t
        if self._is_processing.is_set():
            return True
        if self._pending:
            return True
        if self._last_user_inbound_at == 0.0:
            return False
        return _t.monotonic() - self._last_user_inbound_at < REMINDER_QUIET_SECONDS

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
        # Skip synthetic reminders (mid=0) — no human waiting on them, so
        # the progress watchdog and turn-start typing indicator should
        # both be silent for reminder-only turns.
        self._active_chats = {m.chat_id for m in batch if m.message_id > 0}
        self._active_triggers = {
            m.chat_id: m.message_id for m in batch if m.message_id > 0
        }
        self._replied_chats_this_turn.clear()
        self._dropped_text_retries = 0
        xml = await format_messages_with_context(batch, self._db)
        log.info("starting turn with %d msgs", len(batch))
        # Show "typing..." in every chat involved in this batch.
        await self._start_typing(set(self._active_chats))
        import time as _t
        now = _t.monotonic()
        oldest_receipt = min(
            (m.received_at_monotonic for m in batch if m.received_at_monotonic is not None),
            default=now,
        )
        log.info(
            "hot-path stage=worker-send chats=%s msgs=%d t_ms=%d",
            sorted(self._active_chats), len(batch),
            int((now - oldest_receipt) * 1000),
        )
        await self._worker.send(xml)

    async def _maybe_inject(self) -> None:
        """Write pending messages to CC's stdin mid-turn.

        Called from :meth:`submit` whenever a new message arrives while a
        turn is already running. The worker's ``inject`` is event-driven
        (direct stdin write), not polled, so the follow-up lands at CC's
        next message boundary — typically the next reasoning step. The
        dispatcher's ``_on_message`` awaits this, so we must not do slow
        work here: the only I/O is the DB reply-chain lookup and the
        stdin drain, both ~microseconds for normal payloads.
        """
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
        xml = await format_messages_with_context(batch, self._db)
        await self._worker.inject(xml)

        import time as _t
        now = _t.monotonic()
        oldest_receipt = min(
            (m.received_at_monotonic for m in batch if m.received_at_monotonic is not None),
            default=now,
        )
        log.info(
            "hot-path stage=inject chats=%s msgs=%d t_ms=%d",
            sorted({m.chat_id for m in batch}), len(batch),
            int((now - oldest_receipt) * 1000),
        )

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

    def prime_typing(self, chat_id: int) -> None:
        """Early typing fire from the dispatcher, before debounce + submit.

        Called by :class:`TelegramDispatcher` the moment an allowed,
        non-rate-limited message arrives. Without this, the user waits for
        debounce + XML format + ``worker.send`` before the "typing..."
        indicator renders.

        Fire-and-forget: spawns the Telegram API call as a background task
        so the dispatcher never blocks. Idempotent — if the chat is already
        covered by the refresh loop, no extra API call is made.
        """
        if self._typing_action is None:
            return
        import time

        is_new_chat = chat_id not in self._typing_chats
        if not self._typing_chats:
            # First chat of a fresh turn — anchor the min-visible clock.
            self._typing_started_at = time.monotonic()
        self._typing_chats.add(chat_id)

        if is_new_chat:
            action = self._typing_action
            asyncio.create_task(
                self._safe_typing_call(action, chat_id),
                name=f"pyclaudir-typing-prime-{chat_id}",
            )

        if self._typing_task is None or self._typing_task.done():
            self._typing_wake.clear()
            self._typing_task = asyncio.create_task(
                self._typing_refresh_loop(), name="pyclaudir-typing"
            )

    async def _safe_typing_call(self, action: TypingAction, chat_id: int) -> None:
        try:
            await action(chat_id)
        except Exception as exc:
            log.warning("prime_typing failed for chat %s: %s", chat_id, exc)

    async def _start_typing(self, chat_ids: set[int]) -> None:
        """Ensure typing is live for ``chat_ids``. Idempotent.

        If the refresh loop is already running (e.g. dispatcher called
        :meth:`prime_typing` first), extends coverage to any new chats in
        the batch without resetting ``_typing_started_at`` — that would
        break ``MIN_TYPING_VISIBLE_SECONDS``. Otherwise starts fresh.
        """
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

        loop_running = self._typing_task is not None and not self._typing_task.done()
        if loop_running:
            new_chats = chat_ids - self._typing_chats
            if not new_chats:
                return
            self._typing_chats.update(new_chats)
            for chat_id in new_chats:
                try:
                    await self._typing_action(chat_id)
                except Exception as exc:
                    log.warning("typing action failed for chat %s: %s", chat_id, exc)
            return

        self._typing_chats = set(chat_ids)
        self._typing_wake.clear()
        self._typing_started_at = time.monotonic()
        await self._fire_typing_once()
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
        # Always track the reply, even if typing was already stopped —
        # the progress watchdog uses this to skip chats that have
        # already seen a reply.
        self._replied_chats_this_turn.add(chat_id)

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
        """Refresh typing every ``TYPING_REFRESH_SECONDS`` (the first call
        already fired in start).

        Telegram's typing action expires server-side after ~5s, so we
        refresh on the same cadence to keep the indicator continuous. The
        first call has already been awaited synchronously by
        :meth:`_start_typing`, so this loop only handles the *subsequent*
        ticks.

        Between refreshes we ``wait_for`` the wake event with the same
        timeout so :meth:`notify_chat_replied` can short-circuit the sleep
        and exit the loop immediately when the model successfully sends a
        message.
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
    # Error notification
    # ------------------------------------------------------------------

    async def _notify_error_to_chats(self, text: str) -> None:
        """Send an error message directly via the bot to every chat that
        was waiting for a response. Bypasses the MCP layer (which is dead
        when we need this). Failures are swallowed — this is best-effort.
        """
        if self._error_notify is None:
            return
        for chat_id in self._active_chats:
            try:
                await self._error_notify(chat_id, text, None)
                log.info("sent error notification to chat %s", chat_id)
            except Exception as exc:
                log.warning("failed to send error notification to %s: %s", chat_id, exc)

    async def _handle_dropped_text(self, result: "TurnResult") -> None:
        """Handle a turn that ended with text but no ``send_message`` call.

        Two outcomes:

        1. **Below the shared failure cap** — inject an ``<error>`` into
           the bot's next turn reminding it to use ``send_message``, so
           a recoverable slip (e.g. it started typing a plain answer)
           self-corrects in one additional turn.
        2. **At or above the cap** — stop nagging the model, surface a
           user-facing message, and drop the turn. The best-available
           diagnostic (classifier match on text blocks, or the raw first
           block) is included so the user understands why.
        """
        self._dropped_text_retries += 1
        max_retries = self._tool_error_max_count

        if self._dropped_text_retries < max_retries:
            # Recoverable — inject the corrective reminder and let the
            # model try again.
            error_xml = (
                "<error>You produced text but did not call send_message. "
                "Use the tool — text content blocks are invisible to the user.</error>"
            )
            await self._worker.send(error_xml)
            self._is_processing.set()
            if self._typing_chats:
                await self._start_typing(set(self._typing_chats))
            return

        # Cap hit. Build the clearest user-facing message we can from
        # what CC gave us.
        user_msg = self._build_dropped_text_user_message(result)
        log.warning(
            "dropped_text retry limit hit (%d/%d); surfacing to user",
            self._dropped_text_retries, max_retries,
        )
        await self._notify_error_to_chats(user_msg)
        self._active_chats.clear()
        self._active_triggers.clear()
        # Reset counter so the *next* user turn starts clean even if the
        # underlying CC issue persists — we don't want to nuke their
        # first follow-up message silently.
        self._dropped_text_retries = 0

    @staticmethod
    def _build_dropped_text_user_message(result: "TurnResult") -> str:
        """Compose a user-facing message for a capped dropped-text failure.

        Prefers a classifier-matched message (e.g. "model unavailable —
        fix PYCLAUDIR_MODEL") over the generic fallback. Either way we
        include a trimmed snippet of CC's own diagnostic so the user
        can see the underlying error, not just a generic apology.
        """
        classification: CcFailureClassification | None = classify_cc_failure(
            result.text_blocks
        )
        if classification is not None:
            user_msg = classification.user_message
            detail = classification.matched_source
        else:
            user_msg = (
                "⚠️ I hit a technical issue and couldn't finish that turn. "
                "Please try again in a moment."
            )
            snippet = (result.text_blocks[0] if result.text_blocks else "").strip()
            if len(snippet) > 400:
                snippet = snippet[:400].rstrip() + "…"
            detail = snippet

        if detail:
            user_msg = f"{user_msg}\n\nDetails:\n{detail}"
        return user_msg

    async def _progress_notify_after(self, delay: float) -> None:
        """Fire once after ``delay`` seconds to tell waiting users the
        bot is still working.

        Skips chats that already received a ``send_message`` reply this
        turn — they've already seen the model is alive. Uses
        ``_error_notify`` (the bot-direct path) rather than the MCP
        server, because MCP may be the thing that's slow.

        Posts as a **reply** to the user's triggering message (tracked
        per-chat in ``_active_triggers``). Threading the notice makes
        the routing correct by construction — the chat the reply
        lands in is determined by the message_id, not by any "most
        recent chat" guess. If a turn batched messages from multiple
        chats, each unreplied chat gets its own threaded notice tied
        to its own message.
        """
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._error_notify is None:
            return
        pending = self._active_chats - self._replied_chats_this_turn
        for chat_id in pending:
            reply_to = self._active_triggers.get(chat_id)
            try:
                await self._error_notify(
                    chat_id,
                    "Working on this — one moment.",
                    reply_to,
                )
                log.info(
                    "sent progress notification to chat %s (reply_to=%s)",
                    chat_id, reply_to,
                )
            except Exception as exc:
                log.warning(
                    "progress notification failed for chat %s: %s",
                    chat_id, exc,
                )

    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    async def _control_loop(self) -> None:
        """Wait for each turn to finish and decide what to do next.

        NOTE: ``_run_one_turn`` blocks the engine until the current turn
        completes. Messages arriving from other chats during a
        long-running turn (e.g. code review) queue in ``_pending`` and
        are dispatched only after the turn returns. See README "Known
        limitations — Single-turn blocking".
        """
        try:
            while not self._stop.is_set():
                if not self._is_processing.is_set():
                    await asyncio.sleep(0.05)
                    continue
                await self._run_one_turn()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            log.exception("engine control loop crashed")

    async def _run_one_turn(self) -> None:
        """Arm the progress watchdog, wait for the worker's result,
        dispatch on the outcome. The outer loop just iterates."""
        progress_task = asyncio.create_task(
            self._progress_notify_after(self._progress_notify_seconds),
            name="pyclaudir-progress-notify",
        )
        try:
            try:
                result: TurnResult = await self._worker.wait_for_result()
            except Exception as exc:
                await self._handle_worker_failure(exc)
                return
            await self._handle_turn_result(result)
        finally:
            if not progress_task.done():
                progress_task.cancel()
            try:
                await progress_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _handle_worker_failure(self, exc: Exception) -> None:
        """CC subprocess died mid-turn. The worker's supervisor handles
        respawning; our job is to tell the user."""
        log.error("turn failed: %s", exc)
        self._is_processing.clear()
        await self._stop_typing()
        await self._notify_error_to_chats(
            "⚠️ Sorry, I ran into a temporary issue. "
            "I'm restarting and will be back in a few seconds."
        )
        self._active_chats.clear()
        self._active_triggers.clear()

    async def _handle_turn_result(self, result: "TurnResult") -> None:
        """Process a successfully-returned :class:`TurnResult`.

        Four outcome paths: tool-error-limit abort (worker scheduled
        termination, ``_on_cc_crash`` notifies), stderr-classified
        failure (rate-limit/auth/quota), dropped-text (no
        ``send_message``), or a clean turn — possibly with a follow-up
        ``sleep`` action and pending messages to kick next.
        """
        self._is_processing.clear()
        await self._stop_typing()

        if result.aborted_reason == "tool-error-limit":
            # Don't notify here — ``_on_cc_crash`` will tell the user
            # when the subprocess exits. Leave ``_active_chats`` alone
            # so the callback knows who to notify.
            log.error("turn aborted: tool-error-limit")
            return

        action = result.control.action if result.control else None
        log.info(
            "turn done (action=%s, dropped_text=%s, text_blocks=%d)",
            action, result.dropped_text, len(result.text_blocks),
        )

        # Best-effort classification: if stderr tells us the failure mode
        # (rate-limit, auth, quota…), surface a targeted message. This is
        # orthogonal to dropped_text handling — a turn can be both
        # rate-limited AND dropped_text, but we only notify once per turn.
        stderr_classification = classify_cc_failure(result.stderr_tail)
        if stderr_classification is not None:
            await self._notify_error_to_chats(
                stderr_classification.user_message
            )

        if result.dropped_text:
            await self._handle_dropped_text(result)
            return

        # Successful turn — reset the dropped-text retry counter.
        self._dropped_text_retries = 0
        self._active_chats.clear()
        self._active_triggers.clear()

        if action == "sleep" and result.control and result.control.sleep_ms:
            await asyncio.sleep(result.control.sleep_ms / 1000)

        # If new messages arrived while we were processing, kick them now.
        async with self._lock:
            has_pending = bool(self._pending)
        if has_pending:
            await self._kick()
