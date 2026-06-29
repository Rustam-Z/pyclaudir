"""The engine — debouncer, queue, inject channel, control loop.

This is the heart of hamroh. The dispatcher calls :meth:`Engine.submit`
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
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from ..cc_failure_classifier import CcFailureClassification, classify_cc_failure
from ..config import Config
from ..db.messages import mark_messages_consumed, mark_messages_processed
from ..formatting import chunk_text
from ..models import ChatMessage
from .format import format_messages_with_context
from .restore import build_restored_context
from .typing_indicator import TypingAction, TypingIndicatorMixin, TypingState

#: Async callable shape: ``await error_notify(chat_id, text)`` sends a
#: message directly via the bot, bypassing the MCP layer (which is
#: dead when we need this). Engine doesn't import telegram.
ErrorNotify = Callable[[int, str], Awaitable[None]]

#: A per-turn success/failure hook: ``await hook()``. Aliased so signatures
#: stay readable (and so naive comma-counting param linters don't trip on the
#: ``Callable[[], ...]`` brackets).
AsyncHook = Callable[[], Awaitable[None]]

if TYPE_CHECKING:  # pragma: no cover
    from ..cc_worker import CcWorker, TurnResult
    from ..db.database import Database

log = logging.getLogger("hamroh.engine")


def _trim_snippet(text: str, limit: int = 400) -> str:
    """Trim diagnostic text to fit comfortably in a Telegram message."""
    snippet = text.strip()
    if len(snippet) > limit:
        snippet = snippet[:limit].rstrip() + "…"
    return snippet


def _classified_failure_message(classification: CcFailureClassification) -> str:
    """User-facing message when dropped text is actually a known failure
    (bad model name, quota, …), with the matched diagnostic snippet so the
    user sees what went wrong rather than a generic apology."""
    msg = classification.user_message
    if classification.matched_source:
        msg = f"{msg}\n\nDetails:\n{classification.matched_source}"
    return msg


@dataclass
class TurnState:
    """Per-turn user-facing state. Cleared on each new turn in ``_kick``,
    consulted by the dropped-text handler and crash-notification path."""

    #: Chats from the most recent batch waiting on a reply. Synthetic
    #: reminders (``message_id == 0``) are excluded so reminder-only
    #: turns produce no turn-start typing indicator.
    active_chats: set[int] = field(default_factory=set)
    #: Per active chat, the most recent human ``message_id`` in the batch.
    #: The status heartbeat replies to this so its "still working" updates
    #: thread under the message that kicked the turn instead of floating free.
    reply_targets: dict[int, int] = field(default_factory=dict)
    #: ``time.monotonic()`` when the current turn started in ``_kick``.
    #: Read by :attr:`Engine.turn_elapsed_s` for the /health readout.
    started_monotonic: float = 0.0
    #: ``(chat_id, message_id)`` keys consumed by this turn (kick plus
    #: mid-turn injects). Committed via ``mark_messages_processed`` only
    #: when the turn finishes cleanly — failed/aborted turns leave the
    #: rows untrusted, which bars them from restored-context digests.
    consumed_keys: list[tuple[int, int]] = field(default_factory=list)
    #: True when this turn's opening send carried a ``<restored_context>``
    #: digest. One-shot poison guard: if such a turn fails with an
    #: ``api_error``, the next reset must NOT rebuild the digest.
    had_restored_context: bool = False


@dataclass
class TurnCallbacks:
    """A submit's deferred hooks, kept as a pair so success and failure
    can't desync.

    ``on_success`` runs after the turn that consumed the message ends with
    a result from CC; ``on_failure`` runs when that turn is discarded
    mid-flight (subprocess crash, owner session reset). The reminder loop
    hangs advance/close on success and revert on failure off these — see
    #22 and the claim model in ``db/reminders.py``.
    """

    on_success: AsyncHook
    on_failure: AsyncHook | None = None


@dataclass(frozen=True)
class EngineOptions:
    """Optional wiring + tuning for :class:`Engine`, all with safe defaults so
    a test can build one from just a worker and config.

    ``typing_action`` shows the "typing…" indicator (wired by ``__main__`` to
    ``bot.send_chat_action``); ``error_notify`` sends a message straight via the
    bot when the MCP path is dead.
    """

    debounce_ms: int = 1000
    db: "Database | None" = None
    typing_action: TypingAction | None = None
    error_notify: ErrorNotify | None = None


class Engine(TypingIndicatorMixin):
    def __init__(
        self,
        worker: "CcWorker",
        config: Config,
        options: EngineOptions = EngineOptions(),
    ) -> None:
        self._worker = worker
        self._debounce = options.debounce_ms / 1000.0
        self._db = options.db
        self._typing_action = options.typing_action
        self._error_notify = options.error_notify
        #: Per-turn user state — see :class:`TurnState`.
        self._turn = TurnState()
        #: ``<restored_context>`` digest set via :meth:`stash_restore_context`
        #: on the three session-reset paths; consumed once by the next
        #: ``_kick`` so the fresh session's first turn carries it.
        self._restore_context: str | None = None
        #: Typing-indicator state — see :class:`TypingState`.
        self._typing = TypingState()
        self._pending: list[ChatMessage] = []
        #: Per-submit success+failure hooks queued alongside ``_pending``.
        #: Transferred to ``_turn_callbacks`` when the buffer drains into
        #: a turn (``_kick`` / ``_maybe_inject``). The reminder loop hangs
        #: advance/close (success) and revert (failure) off these so a
        #: subprocess crash mid-turn doesn't lose the reminder — see #22.
        self._pending_callbacks: list[TurnCallbacks] = []
        #: Hooks bound to the in-flight turn. ``on_success`` fires in
        #: ``_fire_turn_callbacks`` once the turn ends cleanly; ``on_failure``
        #: fires in ``_fail_turn_callbacks`` when the turn is discarded
        #: mid-flight (worker crash, session reset) so the caller re-arms
        #: the reminder for the next 60s tick.
        self._turn_callbacks: list[TurnCallbacks] = []
        self._lock = asyncio.Lock()
        self._is_processing = asyncio.Event()
        self._debounce_task: asyncio.Task[None] | None = None
        self._control_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._control_task = asyncio.create_task(
            self._control_loop(), name="hamroh-engine-loop"
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
        # Drop any queued reminder callbacks. A claimed reminder is left
        # ``processing`` in the DB; the next startup's reset_stuck_reminders
        # re-arms it to ``pending`` so the reminder loop re-fires it — the
        # right behaviour for a clean shutdown.
        self._pending_callbacks = []
        self._turn_callbacks = []

    async def reset_session(self) -> None:
        """Owner-requested fresh CC session — delegates to the worker.

        If a turn is in flight, the worker queues a sentinel result that
        unblocks the control loop; ``_handle_turn_result`` cleans up the
        engine-side turn state when it arrives.
        """
        await self._worker.reset_session()

    async def stash_restore_context(self, reason: str) -> None:
        """Build a ``<restored_context>`` digest now; the next ``_kick``
        prepends it to the fresh session's first turn. Stays ``None``
        (plain turn) when there is no DB or no trusted history."""
        self._restore_context = await build_restored_context(self._db, reason=reason)

    # ------------------------------------------------------------------
    # Introspection (read-only, used by /health)
    # ------------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        """Number of buffered messages waiting for the next turn."""
        return len(self._pending)

    @property
    def turn_elapsed_s(self) -> float | None:
        """Seconds the current turn has been running, or None when idle."""
        if not self._is_processing.is_set():
            return None
        return time.monotonic() - self._turn.started_monotonic

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def submit(
        self,
        msg: ChatMessage,
        *,
        on_success: AsyncHook | None = None,
        on_failure: AsyncHook | None = None,
    ) -> None:
        """Add an inbound message to the pending buffer.

        - If the engine is *not* currently processing a turn we (re)start the
          debounce timer; once it fires we drain the buffer and start a turn.
        - If the engine *is* processing a turn we still buffer here, but the
          control loop will drain whatever's in the buffer between turns. The
          inject path is used for *immediate* mid-turn delivery only when
          we're sure CC is mid-stream — see :meth:`_maybe_inject`.

        ``on_success`` / ``on_failure``: optional async hooks for the turn
        that consumes this message. ``on_success`` runs once the turn ends
        with a result from CC; ``on_failure`` runs when the turn is
        discarded before CC consumed it (subprocess crash, owner session
        reset). The reminder loop uses them to advance/close vs revert its
        claimed DB row, so a crash mid-turn doesn't lose the reminder and a
        long turn doesn't re-fire it (see #22).
        """
        async with self._lock:
            self._pending.append(msg)
            if on_success is not None:
                self._pending_callbacks.append(
                    TurnCallbacks(on_success=on_success, on_failure=on_failure)
                )

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
            self._turn_callbacks.extend(self._pending_callbacks)
            self._pending_callbacks = []
            self._is_processing.set()
        # Skip synthetic reminders (mid=0) — no human waiting on them, so
        # the turn-start typing indicator should be silent for
        # reminder-only turns.
        self._turn.active_chats = {m.chat_id for m in batch if m.message_id > 0}
        self._turn.reply_targets = {
            m.chat_id: m.message_id for m in batch if m.message_id > 0
        }
        self._turn.started_monotonic = time.monotonic()
        self._turn.consumed_keys = []
        restore = self._restore_context
        self._restore_context = None
        self._turn.had_restored_context = restore is not None
        xml = await format_messages_with_context(batch, self._db)
        if restore is not None:
            xml = f"{restore}\n{xml}"
        log.info("starting turn with %d msgs", len(batch))
        # Show "typing..." in every chat involved in this batch.
        await self._start_typing(set(self._turn.active_chats))
        self._log_hot_path("worker-send", batch, self._turn.active_chats)
        await self._worker.send(xml)
        await self._mark_consumed(batch)

    def _log_hot_path(
        self, stage: str, batch: list[ChatMessage], chats: set[int]
    ) -> None:
        """Log inbound→worker latency for the batch, keyed on the oldest
        message's receipt time (synthetic messages without one are ignored)."""
        now = time.monotonic()
        oldest_receipt = min(
            (
                m.received_at_monotonic
                for m in batch
                if m.received_at_monotonic is not None
            ),
            default=now,
        )
        log.info(
            "hot-path stage=%s chats=%s msgs=%d t_ms=%d",
            stage,
            sorted(chats),
            len(batch),
            int((now - oldest_receipt) * 1000),
        )

    async def _mark_consumed(self, batch: list[ChatMessage]) -> None:
        """Flag the drained batch as handed to CC.

        Called AFTER the send/inject — CC already has the messages, so
        this one small UPDATE per turn never delays the response. A crash
        in the window between send and this write replays the batch on
        the next boot (at-least-once, mirroring the reminder semantics
        of #22). Synthetic reminders (``message_id == 0``) are skipped —
        they re-fire via their own ``pending`` status.
        """
        keys = [(m.chat_id, m.message_id) for m in batch if m.message_id > 0]
        self._turn.consumed_keys.extend(keys)
        if self._db is None or not keys:
            return
        await mark_messages_consumed(self._db, keys)

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
            self._turn_callbacks.extend(self._pending_callbacks)
            self._pending_callbacks = []
        xml = await format_messages_with_context(batch, self._db)
        await self._worker.inject(xml)
        await self._mark_consumed(batch)
        self._log_hot_path("inject", batch, {m.chat_id for m in batch})
        await self._rearm_typing({m.chat_id for m in batch})

    async def _rearm_typing(self, new_chats: set[int]) -> None:
        """Re-show "typing…" for chats whose follow-up was injected mid-turn.

        Two cases: if the typing loop is still running (model hasn't replied
        yet) we just widen its chat set; if it already exited (the model sent
        its first reply and ``notify_chat_replied`` stopped it) we restart it
        from scratch — same path as a fresh turn.
        """
        if self._typing.task is not None and not self._typing.task.done():
            self._typing.chats.update(new_chats)
        else:
            await self._start_typing(new_chats)

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
        for chat_id in self._turn.active_chats:
            try:
                await self._error_notify(chat_id, text)
                log.info("sent error notification to chat %s", chat_id)
            except Exception as exc:
                log.warning("failed to send error notification to %s: %s", chat_id, exc)

    async def _handle_dropped_text(self, result: "TurnResult") -> None:
        """Deliver text the model produced but never sent via ``telegram_send_message``.

        The model occasionally writes its reply as a plain text content
        block and then stops, instead of calling ``telegram_send_message`` /
        ``telegram_reply_to_message`` — those blocks are invisible to the user.
        Rather than burn a whole retry turn nagging it to resend, deliver
        the text it already produced.

        Exception: when that text is actually a technical error (bad model
        name, quota, …) we surface the classified user-facing message
        instead of echoing the raw diagnostic. ``classify_cc_failure``
        draws the line; thinking blocks never reach ``text_blocks`` (they're
        logged, not collected), so what remains is genuine reply prose.
        """
        classification = classify_cc_failure(result.text_blocks)
        if classification is not None:
            await self._notify_error_to_chats(
                _classified_failure_message(classification)
            )
        else:
            await self._deliver_text_to_chats("\n\n".join(result.text_blocks))
        self._turn.active_chats.clear()
        await self._fire_turn_callbacks()

    async def _deliver_text_to_chats(self, text: str) -> None:
        """Deliver a model reply that landed as a text block to every chat
        waiting on this turn.

        Reuses the error-notify bot channel (the engine never imports
        telegram) and the shared ``chunk_text`` splitter so a long answer
        breaks at paragraph boundaries instead of hitting Telegram's
        length limit. Best-effort: a failed send is logged, not raised.
        """
        if self._error_notify is None or not text.strip():
            return
        chunks = chunk_text(text)
        for chat_id in self._turn.active_chats:
            for chunk in chunks:
                try:
                    await self._error_notify(chat_id, chunk)
                except Exception as exc:
                    log.warning("dropped-text delivery to %s failed: %s", chat_id, exc)
            log.info(
                "delivered dropped text to chat %s (%d chunk(s))",
                chat_id,
                len(chunks),
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
        """Wait for the worker's result and dispatch on the outcome.
        The outer loop just iterates."""
        try:
            result: TurnResult = await self._worker.wait_for_result()
        except Exception as exc:
            await self._handle_worker_failure(exc)
            return
        await self._handle_turn_result(result)

    async def _handle_worker_failure(self, exc: Exception) -> None:
        """CC subprocess died mid-turn. The worker's supervisor handles
        respawning; our job is to tell the user.

        Queued ``on_failure`` hooks fire so the caller (reminder loop)
        reverts its claimed row to ``pending`` and retries — without this,
        a reminder injected into a turn that crashed before CC consumed it
        would be silently lost (#22).
        """
        log.error("turn failed: %s", exc)
        self._is_processing.clear()
        await self._stop_typing()
        if self._turn_callbacks:
            log.info(
                "reverting %d turn callback(s) on worker failure — caller will retry",
                len(self._turn_callbacks),
            )
            await self._fail_turn_callbacks()
        await self._notify_error_to_chats(
            "⚠️ Sorry, I ran into a temporary issue. "
            "I'm restarting and will be back in a few seconds."
        )
        self._turn.active_chats.clear()

    async def _fire_turn_callbacks(self) -> None:
        """Run every ``on_success`` hook queued for the just-ended turn.

        Called from ``_handle_turn_result`` once the turn definitively
        ends — clean stop, dropped-text delivery, tool-error-limit abort,
        or api-error. CC saw the messages, so reminders advance/close.
        Each hook is independent; one failing doesn't suppress the rest.
        See #22.
        """
        callbacks = self._turn_callbacks
        self._turn_callbacks = []
        for cb in callbacks:
            try:
                await cb.on_success()
            except Exception:
                log.exception("turn-success callback failed")

    async def _fail_turn_callbacks(self) -> None:
        """Run every ``on_failure`` hook for a turn discarded before CC
        consumed it (subprocess crash, owner session reset).

        Mirrors :meth:`_fire_turn_callbacks`. The reminder loop hangs a
        ``revert_reminder`` here so a claimed reminder returns to
        ``pending`` and re-fires on the next loop tick — the #22
        at-least-once guarantee, preserved now that the row is claimed.
        Each hook is independent; one failing doesn't suppress the rest.
        """
        callbacks = self._turn_callbacks
        self._turn_callbacks = []
        for cb in callbacks:
            if cb.on_failure is None:
                continue
            try:
                await cb.on_failure()
            except Exception:
                log.exception("turn-failure callback failed")

    async def _handle_aborted_turn(self, result: "TurnResult") -> None:
        """Handle a short-circuited turn (``aborted_reason`` set).

        - ``session-reset``: owner asked for a fresh session mid-turn.
          CC never finished the turn, so ``on_failure`` fires — reminders
          revert to ``pending`` and re-fire into the fresh session on the
          next loop tick (#22).
        - ``tool-error-limit``: don't notify here — ``_on_cc_crash``
          tells the user when the subprocess exits, so ``active_chats``
          stays intact for that callback. CC saw the messages before
          the abort, so callbacks fire — reminders advance and don't
          loop on a poisoned state.
        Neither path kicks ``_pending`` — the subprocess is mid-respawn.
        """
        if result.aborted_reason == "session-reset":
            log.info("turn aborted: session reset requested by owner")
            await self._fail_turn_callbacks()
            self._turn.active_chats.clear()
            return
        log.error("turn aborted: %s", result.aborted_reason)
        await self._fire_turn_callbacks()

    async def _handle_api_error_turn(self, result: "TurnResult") -> None:
        """Handle a turn the API itself rejected (``api_error`` set).

        Skips the dropped-text retry loop — re-sending into the same
        session just re-triggers the same refusal. A classified failure
        (rate-limit, auth, quota…) keeps the session: a reset wouldn't
        fix it and would lose context. Anything else (usage-policy
        refusal, context overflow) has poisoned the session history —
        every resumed turn replays the rejected content — so we notify
        the user and respawn CC fresh. Callbacks fire either way: CC saw
        the messages and retrying identical content fails
        deterministically (tool-error-limit precedent). ``_pending`` is
        not kicked — the subprocess is mid-respawn.
        """
        log.error("turn failed with API error: %s", result.api_error)
        classification = classify_cc_failure(
            [result.api_error or "", *result.text_blocks]
        )
        if classification is not None:
            await self._notify_error_to_chats(classification.user_message)
            self._turn.active_chats.clear()
            await self._fire_turn_callbacks()
            return

        notice = await self._prepare_api_error_reset()
        detail = _trim_snippet(result.api_error or "")
        await self._notify_error_to_chats(f"{notice}\n\nDetails:\n{detail}")
        self._turn.active_chats.clear()
        await self._fire_turn_callbacks()
        await self.reset_session()

    async def _prepare_api_error_reset(self) -> str:
        """One-shot poison guard + digest stash for the api-error reset.

        If the failed turn itself opened with a restored digest, the
        digest is the prime poison suspect: skip the rebuild so the next
        session starts plain (no refusal loop). Otherwise stash a digest
        — the failed turn's own batch was never committed
        (``processed=0``), so the digest can't contain it. Returns the
        matching user-facing notice.
        """
        if self._turn.had_restored_context:
            log.warning(
                "restored digest preceded this api_error; next session starts plain"
            )
            return (
                "⚠️ Claude rejected that request and the turn failed. "
                "I've started a completely fresh session — the recent recap "
                "could not be carried over (it may itself have caused the "
                "failure). Please rephrase and resend."
            )
        await self.stash_restore_context("api-error")
        return (
            "⚠️ Claude rejected that request and the turn failed. "
            "I've started a fresh session and will carry a short recap of "
            "the recent conversation into it. Please rephrase and resend."
        )

    async def _handle_turn_result(self, result: "TurnResult") -> None:
        """Process a successfully-returned :class:`TurnResult`.

        Five outcome paths: short-circuited turn (``aborted_reason``),
        API-rejected turn (``api_error``), stderr-classified failure
        (rate-limit/auth/quota), dropped-text (no ``telegram_send_message``),
        or a clean turn (see :meth:`_finish_clean_turn`).
        """
        self._is_processing.clear()
        await self._stop_typing()

        if result.aborted_reason is not None:
            await self._handle_aborted_turn(result)
            return

        if result.api_error:
            await self._handle_api_error_turn(result)
            return

        action = result.control.action if result.control else None
        log.info(
            "turn done (action=%s, dropped_text=%s, text_blocks=%d)",
            action,
            result.dropped_text,
            len(result.text_blocks),
        )

        # Best-effort classification: if stderr tells us the failure mode
        # (rate-limit, auth, quota…), surface a targeted message. This is
        # orthogonal to dropped_text handling — a turn can be both
        # rate-limited AND dropped_text, but we only notify once per turn.
        stderr_classification = classify_cc_failure(result.stderr_tail)
        if stderr_classification is not None:
            await self._notify_error_to_chats(stderr_classification.user_message)

        if result.dropped_text:
            await self._handle_dropped_text(result)
            return

        await self._finish_clean_turn(result, action)

    async def _finish_clean_turn(
        self, result: "TurnResult", action: str | None
    ) -> None:
        """Wrap up a successful turn: commit its messages as trusted, fire
        callbacks, honour a ``sleep`` action, and kick any messages that
        queued up while the turn was running.

        The commit is the ONLY place rows gain ``processed=1`` — failed,
        aborted, and crashed turns never reach it, so their messages stay
        barred from restored-context digests.
        """
        if self._db is not None and self._turn.consumed_keys:
            await mark_messages_processed(self._db, self._turn.consumed_keys)
        self._turn.active_chats.clear()
        await self._fire_turn_callbacks()

        if action == "sleep" and result.control and result.control.sleep_ms:
            await asyncio.sleep(result.control.sleep_ms / 1000)

        async with self._lock:
            has_pending = bool(self._pending)
        if has_pending:
            await self._kick()
