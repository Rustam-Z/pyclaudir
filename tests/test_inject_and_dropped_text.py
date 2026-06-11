"""Step 9 invariants: inject mechanism and dropped-text detection."""

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
    eng = Engine(worker, _CFG, debounce_ms=20)
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
async def test_message_during_dropped_text_recovery_injects_not_kicks() -> None:
    """A message arriving while the corrective reminder is being written
    must take the inject path — not see an idle engine and start a
    second concurrent turn (the engine must look busy across that await)."""

    class SlowRecoveryWorker(FakeWorker):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def send(self, text: str) -> None:
            await super().send(text)
            if "did not call send_message" in text:
                await self.release.wait()

    worker = SlowRecoveryWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1

        # Turn ends with dropped text → engine starts the corrective send,
        # which blocks until we release it.
        worker.feed(TurnResult(
            text_blocks=["plain text"], control=None, dropped_text=True,
        ))
        await asyncio.sleep(0.05)

        # Follow-up lands mid-recovery, then the send unblocks. Wait past
        # the debounce so a wrongly-armed kick would have fired.
        await eng.submit(_msg("follow-up", mid=2))
        worker.release.set()
        await asyncio.sleep(0.08)

        new_turns = [s for s in worker.sent if "follow-up" in s]
        assert new_turns == [], "follow-up started a second turn instead of injecting"
        joined = "\n".join(worker.injected)
        assert "follow-up" in joined, "follow-up was not injected into the live turn"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_dropped_text_retry_limit_notifies_user() -> None:
    """After ``TOOL_ERROR_MAX_COUNT`` consecutive drops, the engine must
    stop nagging the model and instead surface the failure to the user
    via the error_notify path — including the dropped text snippet so
    the user sees what actually went wrong (e.g. a bad model name).

    Uses the *same* threshold as the tool-error circuit breaker
    (``Config.tool_error_max_count`` / ``PYCLAUDIR_TOOL_ERROR_MAX_COUNT``,
    default 3) so operators tune one knob for both failure modes.
    """
    max_retries = _CFG.tool_error_max_count

    worker = FakeWorker()
    notifications: list[tuple[int, str]] = []

    async def capture_notify(chat_id: int, text: str) -> None:
        notifications.append((chat_id, text))

    eng = Engine(worker, _CFG, debounce_ms=20, error_notify=capture_notify)
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
        chat_id, text = notifications[0]
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
    max_retries = _CFG.tool_error_max_count
    below_cap = max_retries - 1

    worker = FakeWorker()
    notifications: list[tuple[int, str]] = []

    async def capture_notify(chat_id: int, text: str) -> None:
        notifications.append((chat_id, text))

    eng = Engine(worker, _CFG, debounce_ms=20, error_notify=capture_notify)
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
async def test_health_introspection_accessors() -> None:
    """``pending_count`` / ``turn_elapsed_s`` back the /health readout."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    await eng.start()
    try:
        assert eng.turn_elapsed_s is None, "idle engine reports no running turn"
        assert eng.pending_count == 0, "fresh engine has an empty buffer"

        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)  # debounce fires, turn starts
        elapsed = eng.turn_elapsed_s
        assert elapsed is not None and elapsed >= 0, "running turn reports elapsed time"

        worker.feed(TurnResult(
            text_blocks=[],
            control=ControlAction(action="stop", reason="ok"),
            dropped_text=False,
        ))
        await asyncio.sleep(0.05)
        assert eng.turn_elapsed_s is None, "finished turn reports idle again"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_pending_count_reflects_buffered_messages() -> None:
    """Messages waiting on a long debounce are visible as queue depth."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=5000)
    await eng.start()
    try:
        await eng.submit(_msg("queued", mid=1))
        assert eng.pending_count == 1, "buffered message must show as queued"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_inject_drained_between_turns_when_pending() -> None:
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
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
