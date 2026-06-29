"""Step 9 invariants: inject mechanism and dropped-text detection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from pathlib import Path

from hamroh.cc_worker import TurnResult
from hamroh.config import Config
from hamroh.engine import Engine, EngineOptions
from hamroh.models import ChatMessage, ControlAction


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
async def test_dropped_text_delivers_answer_to_user() -> None:
    """A turn that ends with a text block but no ``telegram_send_message`` must
    deliver that text to the waiting chat — not burn a retry turn nagging
    the model to resend."""
    worker = FakeWorker()
    delivered: list[tuple[int, str]] = []

    async def capture(chat_id: int, text: str) -> None:
        delivered.append((chat_id, text))

    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20, error_notify=capture))
    await eng.start()
    try:
        # Given a user message that starts a turn
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1, "the user turn was handed to the worker"

        # When the turn ends with a text block but no telegram_send_message call
        worker.feed(
            TurnResult(
                text_blocks=["Here is your answer"],
                control=ControlAction(action="stop", reason="answered"),
                dropped_text=True,
            )
        )
        await asyncio.sleep(0.05)

        # Then the text is delivered as-is to the waiting chat, and no
        # corrective message is re-sent to the worker (no wasted retry).
        assert delivered == [(-100, "Here is your answer")], (
            f"answer was not delivered to the user; got {delivered!r}"
        )
        assert len(worker.sent) == 1, "a retry turn was wrongly kicked into the worker"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_dropped_text_classified_failure_surfaces_error() -> None:
    """When the dropped text is actually a technical failure (e.g. a bad
    model name), surface the classified guidance — not the raw diagnostic
    echoed back as if it were a real answer."""
    worker = FakeWorker()
    notifications: list[tuple[int, str]] = []

    async def capture_notify(chat_id: int, text: str) -> None:
        notifications.append((chat_id, text))

    eng = Engine(
        worker, _CFG, EngineOptions(debounce_ms=20, error_notify=capture_notify)
    )
    await eng.start()
    try:
        # Given a turn whose only text block is a model-access error
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)
        diagnostic = (
            "There's an issue with the selected model (claude-sonnet-4-7). "
            "It may not exist or you may not have access to it."
        )

        # When that turn is reported as dropped text
        worker.feed(
            TurnResult(
                text_blocks=[diagnostic],
                control=None,
                dropped_text=True,
            )
        )
        await asyncio.sleep(0.05)

        # Then exactly one targeted notification is shown (not the raw text),
        # and no retry turn is kicked.
        assert len(notifications) == 1, "exactly one user-facing notification"
        chat_id, text = notifications[0]
        assert chat_id == -100
        assert "hamroh_model" in text.lower(), "classified guidance shown to user"
        assert "claude-sonnet-4-7" in text, "diagnostic snippet preserved for the user"
        assert len(worker.sent) == 1, "no retry turn kicked into the worker"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_health_introspection_accessors() -> None:
    """``pending_count`` / ``turn_elapsed_s`` back the /health readout."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        assert eng.turn_elapsed_s is None, "idle engine reports no running turn"
        assert eng.pending_count == 0, "fresh engine has an empty buffer"

        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)  # debounce fires, turn starts
        elapsed = eng.turn_elapsed_s
        assert elapsed is not None and elapsed >= 0, "running turn reports elapsed time"

        worker.feed(
            TurnResult(
                text_blocks=[],
                control=ControlAction(action="stop", reason="ok"),
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.05)
        assert eng.turn_elapsed_s is None, "finished turn reports idle again"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_pending_count_reflects_buffered_messages() -> None:
    """Messages waiting on a long debounce are visible as queue depth."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=5000))
    await eng.start()
    try:
        await eng.submit(_msg("queued", mid=1))
        assert eng.pending_count == 1, "buffered message must show as queued"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_inject_drained_between_turns_when_pending() -> None:
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
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
        worker.feed(
            TurnResult(
                text_blocks=[],
                control=ControlAction(action="stop", reason="ok"),
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()
