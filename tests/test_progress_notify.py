"""Progress notification — tells users the bot is still working on long turns.

Uses a shortened ``PYCLAUDIR_PROGRESS_NOTIFY_SECONDS`` so the tests run
in tens of milliseconds, not a minute.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from pyclaudir.cc_worker import TurnResult
from pyclaudir.engine import Engine
from pyclaudir.models import ChatMessage, ControlAction


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


@pytest.fixture
def short_progress_env():
    os.environ["PYCLAUDIR_PROGRESS_NOTIFY_SECONDS"] = "0.05"
    yield
    del os.environ["PYCLAUDIR_PROGRESS_NOTIFY_SECONDS"]


@pytest.mark.asyncio
async def test_progress_notification_fires_on_long_turn(short_progress_env) -> None:
    """Turn lasts longer than the progress threshold → user gets pinged."""
    worker = FakeWorker()
    notifications: list[tuple[int, str]] = []

    async def fake_error_notify(chat_id: int, text: str) -> None:
        notifications.append((chat_id, text))

    eng = Engine(worker, debounce_ms=10, error_notify=fake_error_notify)
    await eng.start()
    try:
        await eng.submit(_msg("hello", chat_id=-100))
        # Let the turn start and the progress watchdog fire.
        await asyncio.sleep(0.2)
        assert len(notifications) == 1
        chat_id, text = notifications[0]
        assert chat_id == -100
        assert "Still working" in text

        # Finish the turn cleanly so stop() doesn't hang.
        worker.feed_result(
            TurnResult(control=ControlAction(action="stop", reason="ok"))
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_progress_notification_suppressed_if_turn_finishes_fast(
    short_progress_env,
) -> None:
    """Turn completes before the threshold → no progress notification."""
    worker = FakeWorker()
    notifications: list[tuple[int, str]] = []

    async def fake_error_notify(chat_id: int, text: str) -> None:
        notifications.append((chat_id, text))

    eng = Engine(worker, debounce_ms=10, error_notify=fake_error_notify)
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
async def test_progress_notification_skips_replied_chats(
    short_progress_env,
) -> None:
    """If a chat already saw a ``send_message`` reply this turn, skip it."""
    worker = FakeWorker()
    notifications: list[tuple[int, str]] = []

    async def fake_error_notify(chat_id: int, text: str) -> None:
        notifications.append((chat_id, text))

    eng = Engine(worker, debounce_ms=10, error_notify=fake_error_notify)
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
async def test_progress_notification_per_turn_reset(short_progress_env) -> None:
    """``_replied_chats_this_turn`` is cleared between turns."""
    worker = FakeWorker()
    notifications: list[tuple[int, str]] = []

    async def fake_error_notify(chat_id: int, text: str) -> None:
        notifications.append((chat_id, text))

    eng = Engine(worker, debounce_ms=10, error_notify=fake_error_notify)
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
