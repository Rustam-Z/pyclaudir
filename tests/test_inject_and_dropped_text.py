"""Step 9 invariants: inject mechanism and dropped-text detection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from pyclaudir.cc_worker import TurnResult
from pyclaudir.engine import Engine
from pyclaudir.models import ChatMessage, ControlAction


def _msg(text: str, mid: int) -> ChatMessage:
    return ChatMessage(
        chat_id=-100,
        message_id=mid,
        user_id=42,
        username="alice",
        first_name="Alice",
        direction="in",
        timestamp=datetime(2026, 4, 11, 10, 31, tzinfo=timezone.utc),
        text=text,
    )


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.injected: list[str] = []
        self._results: asyncio.Queue[TurnResult] = asyncio.Queue()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def inject(self, text: str) -> None:
        self.injected.append(text)

    async def wait_for_result(self) -> TurnResult:
        return await self._results.get()

    def feed(self, result: TurnResult) -> None:
        self._results.put_nowait(result)


@pytest.mark.asyncio
async def test_dropped_text_triggers_corrective_send() -> None:
    worker = FakeWorker()
    eng = Engine(worker, debounce_ms=20)
    await eng.start()
    try:
        # First inbound message → triggers a turn
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1

        # Worker reports dropped text (model wrote text but never called send_message)
        worker.feed(TurnResult(
            text_blocks=["I would say hi"],
            control=None,
            dropped_text=True,
        ))
        # Give the control loop a moment to process the result
        await asyncio.sleep(0.05)

        # Engine should have re-sent a corrective message
        assert len(worker.sent) >= 2
        assert "did not call send_message" in worker.sent[1]
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_dropped_text_retry_limit_notifies_user() -> None:
    """After ``TOOL_ERROR_MAX_COUNT`` consecutive drops, the engine must
    stop nagging the model and instead surface the failure to the user
    via the error_notify path — including the dropped text snippet so
    the user sees what actually went wrong (e.g. a bad model name).

    Uses the *same* threshold as the tool-error circuit breaker
    (``PYCLAUDIR_TOOL_ERROR_MAX_COUNT``, default 3) so operators tune one
    knob for both failure modes.
    """
    from pyclaudir.cc_worker import CcWorker

    max_retries = CcWorker.TOOL_ERROR_MAX_COUNT

    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def capture_notify(chat_id: int, text: str, reply_to: int | None) -> None:
        notifications.append((chat_id, text, reply_to))

    eng = Engine(worker, debounce_ms=20, error_notify=capture_notify)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1

        # Feed exactly max_retries drops; the max_retries-th drop trips
        # the breaker (>= threshold) and surfaces to the user.
        diagnostic = (
            "There's an issue with the selected model (claude-sonnet-4-7). "
            "It may not exist or you may not have access to it."
        )
        for _ in range(max_retries):
            worker.feed(TurnResult(
                text_blocks=[diagnostic],
                control=None,
                dropped_text=True,
            ))
            await asyncio.sleep(0.05)

        # (max_retries - 1) corrective injections below the cap, then the
        # max_retries-th drop triggers a user notification instead of
        # another corrective send.
        corrective = [s for s in worker.sent if "did not call send_message" in s]
        assert len(corrective) == max_retries - 1

        assert len(notifications) == 1
        chat_id, text, _reply_to = notifications[0]
        assert chat_id == -100
        # The classifier recognised the model-access pattern, so the
        # message is targeted (mentions PYCLAUDIR_MODEL) rather than
        # the generic "technical issue" fallback.
        assert "pyclaudir_model" in text.lower()
        assert "claude-sonnet-4-7" in text  # the diagnostic snippet survives
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_dropped_text_counter_resets_on_new_turn() -> None:
    """A successful turn (or a fresh user turn) must reset the drop
    counter so historical drops don't poison future turns."""
    from pyclaudir.cc_worker import CcWorker

    max_retries = CcWorker.TOOL_ERROR_MAX_COUNT
    below_cap = max_retries - 1

    worker = FakeWorker()
    notifications: list[tuple[int, str, int | None]] = []

    async def capture_notify(chat_id: int, text: str, reply_to: int | None) -> None:
        notifications.append((chat_id, text, reply_to))

    eng = Engine(worker, debounce_ms=20, error_notify=capture_notify)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)

        # Drop below the cap, then a clean stop.
        for _ in range(below_cap):
            worker.feed(TurnResult(
                text_blocks=["oops"], control=None, dropped_text=True,
            ))
            await asyncio.sleep(0.05)
        worker.feed(TurnResult(
            text_blocks=[], control=ControlAction(action="stop", reason="ok"),
            dropped_text=False,
        ))
        await asyncio.sleep(0.05)

        # No user notification yet — counter stayed below threshold.
        assert notifications == []

        # New turn: drop below-cap again. Without the reset we'd be at
        # 2*below_cap across turns and trip the breaker; with the reset
        # the counter restarts at 0.
        await eng.submit(_msg("again", mid=2))
        await asyncio.sleep(0.08)
        for _ in range(below_cap):
            worker.feed(TurnResult(
                text_blocks=["oops"], control=None, dropped_text=True,
            ))
            await asyncio.sleep(0.05)

        assert notifications == []
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_inject_drained_between_turns_when_pending() -> None:
    worker = FakeWorker()
    eng = Engine(worker, debounce_ms=20)
    await eng.start()
    try:
        await eng.submit(_msg("first", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1

        # Two messages arrive while turn is in progress → inject path
        await eng.submit(_msg("mid-a", mid=2))
        await eng.submit(_msg("mid-b", mid=3))
        await asyncio.sleep(0.05)
        # Both injected (the second submit's _maybe_inject drains all pending)
        joined = "\n".join(worker.injected)
        assert "mid-a" in joined and "mid-b" in joined

        # Turn finishes cleanly with stop
        worker.feed(TurnResult(
            text_blocks=[],
            control=ControlAction(action="stop", reason="ok"),
            dropped_text=False,
        ))
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()
