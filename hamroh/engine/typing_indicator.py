"""Typing-indicator control for the engine.

Relocated verbatim from ``engine.py`` in the file-size split.
:class:`TypingIndicatorMixin` is a mixin (not a standalone object)
because the methods read the engine's ``_typing`` state and
``_typing_action`` callback, both defined in ``Engine.__init__``.
Tests poke ``eng._typing.chats`` / ``eng._typing.task`` directly, so
those names are part of the test contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# Pinned to the parent package name so log captures keyed on
# ``"hamroh.engine"`` keep matching after the module split.
log = logging.getLogger("hamroh.engine")

#: How often we re-fire ``send_chat_action`` while a turn is in flight.
#: Telegram's typing action expires after ~5s on the server side; matching
#: that interval keeps the indicator continuous without spamming the API.
TYPING_REFRESH_SECONDS = 5

#: Telegram clients suppress very brief typing displays to avoid flicker —
#: typing that's "live" for less than ~1 second often never visually
#: renders in the user's client. We enforce a minimum visible duration
#: from the moment the first typing call fires, so that even when the
#: model responds in a fraction of a second the user actually sees the
#: indicator. Concretely: ``notify_chat_replied`` defers the actual
#: dismissal until this many seconds have elapsed since typing started.
MIN_TYPING_VISIBLE_SECONDS = 1

#: Async callable shape: ``await typing_action(chat_id)`` should fire one
#: ``send_chat_action`` to that chat. Engine doesn't import telegram.
TypingAction = Callable[[int], Awaitable[None]]


@dataclass
class TypingState:
    """All typing-indicator state for one engine instance.

    Lives on ``Engine._typing``. Tests poke directly at these fields
    (``eng._typing.chats``, ``eng._typing.task``) so the names are
    part of the test contract — rename with care.
    """

    #: Background refresh task. ``None`` between turns.
    task: asyncio.Task[None] | None = None
    #: Chat ids the indicator is currently active for.
    chats: set[int] = field(default_factory=set)
    #: Set whenever the chat set changes — wakes the refresh loop so it
    #: notices a removal without sleeping out the full refresh interval.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    #: ``time.monotonic()`` of the first typing call this turn. Anchors
    #: ``MIN_TYPING_VISIBLE_SECONDS`` so a fast turn 2 still renders.
    started_at: float = 0.0
    #: Background task that defers a discard when ``notify_chat_replied``
    #: fires before the minimum visible duration has elapsed.
    deferred_stop: asyncio.Task[None] | None = None


class TypingIndicatorMixin:
    """Typing-indicator methods mixed into ``Engine``.

    The attributes below are declared (not assigned) so mypy can type the
    mixin's reads; ``Engine.__init__`` is what actually sets them.
    """

    _typing: TypingState
    _typing_action: TypingAction | None

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

        is_new_chat = chat_id not in self._typing.chats
        if not self._typing.chats:
            # First chat of a fresh turn — anchor the min-visible clock.
            self._typing.started_at = time.monotonic()
        self._typing.chats.add(chat_id)

        if is_new_chat:
            action = self._typing_action
            asyncio.create_task(
                self._safe_typing_call(action, chat_id),
                name=f"hamroh-typing-prime-{chat_id}",
            )

        if self._typing.task is None or self._typing.task.done():
            self._typing.wake.clear()
            self._typing.task = asyncio.create_task(
                self._typing_refresh_loop(), name="hamroh-typing"
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
            "None"
            if self._typing.task is None
            else ("done" if self._typing.task.done() else "running"),
        )
        if self._typing_action is None or not chat_ids:
            return

        loop_running = self._typing.task is not None and not self._typing.task.done()
        if loop_running:
            new_chats = chat_ids - self._typing.chats
            if not new_chats:
                return
            self._typing.chats.update(new_chats)
            for chat_id in new_chats:
                try:
                    await self._typing_action(chat_id)
                except Exception as exc:
                    log.warning("typing action failed for chat %s: %s", chat_id, exc)
            return

        self._typing.chats = set(chat_ids)
        self._typing.wake.clear()
        self._typing.started_at = time.monotonic()
        await self._fire_typing_once()
        self._typing.task = asyncio.create_task(
            self._typing_refresh_loop(), name="hamroh-typing"
        )

    def notify_chat_replied(self, chat_id: int) -> None:
        """Called by ``telegram_send_message`` the moment Telegram confirms delivery.

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
        inside the ``telegram_send_message`` tool's coroutine and we don't want
        to introduce an extra ``await`` between message delivery and
        notification.
        """
        if chat_id not in self._typing.chats:
            return

        elapsed = time.monotonic() - self._typing.started_at
        remaining = MIN_TYPING_VISIBLE_SECONDS - elapsed

        if remaining <= 0:
            # Typing has been live long enough; stop immediately.
            self._typing.chats.discard(chat_id)
            self._typing.wake.set()
            return

        # Too fast — defer the discard so the indicator is visible for
        # at least MIN_TYPING_VISIBLE_SECONDS from when it started.
        # During the deferral the typing loop keeps refreshing.
        self._schedule_deferred_discard(chat_id, remaining)

    def _schedule_deferred_discard(self, chat_id: int, delay: float) -> None:
        """Discard ``chat_id`` after ``delay`` seconds via a background task.

        Keeps the typing indicator visible for at least
        :data:`MIN_TYPING_VISIBLE_SECONDS`. Fire-and-forget: we don't await
        the task so :meth:`notify_chat_replied` returns immediately and the
        ``telegram_send_message`` tool isn't blocked.
        """

        async def _deferred_discard() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            self._typing.chats.discard(chat_id)
            self._typing.wake.set()

        self._typing.deferred_stop = asyncio.create_task(
            _deferred_discard(), name="hamroh-typing-deferred-stop"
        )

    async def _stop_typing(self) -> None:
        self._typing.chats.clear()
        self._typing.wake.set()
        # Cancel any pending deferred discard so it doesn't fire after we
        # already stopped.
        if (
            self._typing.deferred_stop is not None
            and not self._typing.deferred_stop.done()
        ):
            self._typing.deferred_stop.cancel()
            try:
                await self._typing.deferred_stop
            except (asyncio.CancelledError, Exception):
                pass
        self._typing.deferred_stop = None
        if self._typing.task is not None and not self._typing.task.done():
            self._typing.task.cancel()
            try:
                await self._typing.task
            except (asyncio.CancelledError, Exception):
                pass
        self._typing.task = None

    async def _fire_typing_once(self) -> None:
        if self._typing_action is None:
            return
        for chat_id in list(self._typing.chats):
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
            while self._typing.chats:
                self._typing.wake.clear()
                try:
                    await asyncio.wait_for(
                        self._typing.wake.wait(),
                        timeout=TYPING_REFRESH_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                if not self._typing.chats:
                    return
                await self._fire_typing_once()
        except asyncio.CancelledError:
            raise
