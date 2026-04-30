"""Progress notification — tells users the bot is still working on long turns.

Uses a shortened ``Engine._progress_notify_seconds`` (see
``_short_progress_engine`` factory below) so the tests run in tens of
milliseconds, not a minute. The knob normally flows from
``Config.progress_notify_seconds`` (env var
``PYCLAUDIR_PROGRESS_NOTIFY_SECONDS``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from pathlib import Path

from pyclaudir.cc_worker import TurnResult
from pyclaudir.config import Config
from pyclaudir.engine import Engine
from pyclaudir.models import ChatMessage, ControlAction


_CFG = Config.for_test(Path("/tmp"))


def _msg(text: str, mid: int = 1, chat_id: int = -100) -> ChatMessage:
    return ChatMessage(
        chat_id=chat_id,
        message_id=mid,
        user_id=42,
        username="alice",
        first_name="Alice",
        direction="in",
        timestamp=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
        text=text,
    )


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.injected: list[str] = []
        self._results: asyncio.Queue = asyncio.Queue()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def inject(self, text: str) -> None:
        self.injected.append(text)

    async def wait_for_result(self):
        return await self._results.get()

    def feed_result(self, result) -> None:
        self._results.put_nowait(result)


#: Tests pass this kwarg into ``Engine`` to shorten the progress
#: watchdog timer. Equivalent to setting
#: ``PYCLAUDIR_PROGRESS_NOTIFY_SECONDS=0.05`` but explicit at the
#: construction site instead of mutating process-wide state.
SHORT_PROGRESS = 0.05


def _short_progress_engine(worker, **kwargs):
    """Build an Engine with the progress watchdog shortened for tests."""
    eng = Engine(worker, _CFG, debounce_ms=10, **kwargs)
    eng._progress_notify_seconds = SHORT_PROGRESS
    return eng


@pytest.mark.asyncio
async def test_progress_notification_fires_on_long_turn() -> None:
    """Turn lasts longer than the progress threshold → user gets pinged."""
    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def fake_error_notify(
        chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        notifications.append((chat_id, text, reply_to_message_id))

    eng = _short_progress_engine(worker, error_notify=fake_error_notify)
    await eng.start()
    try:
        await eng.submit(_msg("hello", mid=7, chat_id=-100))
        # Let the turn start and the progress watchdog fire.
        await asyncio.sleep(0.2)
        assert len(notifications) == 1
        chat_id, text, reply_to = notifications[0]
        assert chat_id == -100
        assert "One moment" in text
        # The notice must be threaded to the user's own triggering
        # message — this is what guarantees it routes to the right
        # chat regardless of what other chats were active.
        assert reply_to == 7

        # Finish the turn cleanly so stop() doesn't hang.
        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_progress_notification_suppressed_if_turn_finishes_fast() -> None:
    """Turn completes before the threshold → no progress notification."""
    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def fake_error_notify(
        chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        notifications.append((chat_id, text, reply_to_message_id))

    eng = _short_progress_engine(worker, error_notify=fake_error_notify)
    await eng.start()
    try:
        await eng.submit(_msg("hello", chat_id=-100))
        # Wait just long enough for the turn to start; finish well
        # before the 50ms progress threshold.
        await asyncio.sleep(0.02)
        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        # Let the control loop process the result and cancel the watchdog.
        await asyncio.sleep(0.15)
        assert notifications == []
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_progress_notification_skips_replied_chats() -> None:
    """If a chat already saw a ``send_message`` reply this turn, skip it."""
    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def fake_error_notify(
        chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        notifications.append((chat_id, text, reply_to_message_id))

    eng = _short_progress_engine(worker, error_notify=fake_error_notify)
    await eng.start()
    try:
        await eng.submit(_msg("hi", chat_id=-100))
        # Turn has started. Before the watchdog fires, simulate the
        # model calling send_message which fires notify_chat_replied.
        await asyncio.sleep(0.02)
        eng.notify_chat_replied(-100)
        # Now let the watchdog delay expire.
        await asyncio.sleep(0.1)
        assert notifications == []

        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_progress_notification_threads_to_each_chats_own_trigger() -> None:
    """Regression: when a turn batches messages from two chats, each
    unreplied chat must get a progress notice threaded to *its own*
    message_id. Previously we just sent to ``_active_chats`` with no
    reply_to, which in the essay-in-DM / short-reply-in-group case
    made the notice appear in the chat where the bot was *not* working.
    """
    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def fake_error_notify(
        chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        notifications.append((chat_id, text, reply_to_message_id))

    eng = _short_progress_engine(worker, error_notify=fake_error_notify)
    await eng.start()
    try:
        # Two inbound messages from two different chats coalesced by debounce.
        await eng.submit(_msg("write me an essay", mid=101, chat_id=-100))
        await eng.submit(_msg("ping", mid=202, chat_id=-200))
        # Model answered only the group — DM still waiting on the essay.
        await asyncio.sleep(0.02)
        eng.notify_chat_replied(-200)
        # Watchdog fires.
        await asyncio.sleep(0.1)

        # Exactly one notice, in the unreplied chat, threaded to its
        # own triggering message (not the other chat's).
        assert len(notifications) == 1
        chat_id, _text, reply_to = notifications[0]
        assert chat_id == -100
        assert reply_to == 101

        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_progress_watchdog_skips_reminder_only_turn() -> None:
    """Synthetic reminders (``message_id=0``) have no human waiting on the
    other end — the cron-triggered self-reflection skill is the canonical
    case. The progress watchdog must stay silent for reminder-only turns
    so it doesn't ping the seeded chat (e.g. owner DM at midnight UTC).
    """
    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def fake_error_notify(
        chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        notifications.append((chat_id, text, reply_to_message_id))

    eng = _short_progress_engine(worker, error_notify=fake_error_notify)
    await eng.start()
    try:
        await eng.submit(_msg("<reminder>...</reminder>", mid=0, chat_id=-100))
        await asyncio.sleep(0.2)
        assert notifications == []
        # _active_chats should not contain the reminder's chat — the same
        # filter that excludes mid=0 from _active_triggers excludes it
        # here too.
        assert -100 not in eng._turn.active_chats

        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_progress_watchdog_pings_only_real_user_when_batched_with_reminder() -> None:
    """If a reminder happens to arrive while a real user message is being
    batched, the watchdog must fire only for the real user — not for the
    reminder's seeded chat. Before the fix both chats were pinged
    because the reminder's chat_id leaked into _active_chats.
    """
    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def fake_error_notify(
        chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        notifications.append((chat_id, text, reply_to_message_id))

    eng = _short_progress_engine(worker, error_notify=fake_error_notify)
    await eng.start()
    try:
        # Reminder seeded at owner DM (-100) coalesces with a real user
        # message in a group (-200, mid=42).
        await eng.submit(_msg("<reminder>...</reminder>", mid=0, chat_id=-100))
        await eng.submit(_msg("hey bot", mid=42, chat_id=-200))
        await asyncio.sleep(0.2)

        # Exactly one ping, to the real user, threaded to their message.
        assert len(notifications) == 1
        chat_id, _text, reply_to = notifications[0]
        assert chat_id == -200
        assert reply_to == 42

        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_progress_notification_per_turn_reset() -> None:
    """``_replied_chats_this_turn`` is cleared between turns."""
    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def fake_error_notify(
        chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        notifications.append((chat_id, text, reply_to_message_id))

    eng = _short_progress_engine(worker, error_notify=fake_error_notify)
    await eng.start()
    try:
        # Turn 1: model replies, no progress notification.
        await eng.submit(_msg("m1", mid=1, chat_id=-100))
        await asyncio.sleep(0.02)
        eng.notify_chat_replied(-100)
        await asyncio.sleep(0.1)
        assert notifications == []
        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        await asyncio.sleep(0.05)

        # Turn 2: model does NOT reply fast enough. The progress watchdog
        # must fire because the replied-set was cleared at kick time.
        await eng.submit(_msg("m2", mid=2, chat_id=-100))
        await asyncio.sleep(0.2)
        assert len(notifications) == 1
        assert notifications[0][0] == -100

        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()
