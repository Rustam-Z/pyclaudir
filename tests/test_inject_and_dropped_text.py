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
