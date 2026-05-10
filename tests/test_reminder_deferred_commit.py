"""Engine-level on_success callback contract that backs reminder
delivery (issue #22).

The reminder loop hangs ``mark_sent`` / ``advance_recurring`` off
:meth:`Engine.submit`'s ``on_success`` hook so the DB row is only
updated after CC actually consumes the turn. These tests pin the four
outcomes the fix depends on:

1. clean turn end → callback fires
2. CC subprocess crash → callback discarded (reminder retried by next loop tick)
3. recoverable dropped-text → callback withheld until the retry turn ends
4. dropped-text cap-hit → callback fires (CC saw the message; retrying is pointless)
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pyclaudir.cc_worker import TurnResult
from pyclaudir.config import Config
from pyclaudir.engine import Engine
from pyclaudir.models import ChatMessage, ControlAction


_CFG = Config.for_test(Path("/tmp"))


def _msg(text: str, mid: int = 1) -> ChatMessage:
    return ChatMessage(
        chat_id=-100,
        message_id=mid,
        user_id=42,
        direction="in",
        timestamp=datetime(2026, 4, 11, 10, 31, tzinfo=timezone.utc),
        text=text,
    )


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.injected: list[str] = []
        self._results: asyncio.Queue[TurnResult | Exception] = asyncio.Queue()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def inject(self, text: str) -> None:
        self.injected.append(text)

    async def wait_for_result(self) -> TurnResult:
        item = await self._results.get()
        if isinstance(item, Exception):
            raise item
        return item

    def feed(self, item: TurnResult | Exception) -> None:
        self._results.put_nowait(item)


@pytest.mark.asyncio
async def test_on_success_fires_after_clean_turn_end() -> None:
    """Happy path: callback runs once the turn ends with action=stop."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    fired: list[int] = []

    async def cb() -> None:
        fired.append(1)

    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1), on_success=cb)
        await asyncio.sleep(0.08)
        assert worker.sent, "turn did not start"
        assert fired == [], "callback fired before turn result"

        worker.feed(
            TurnResult(
                control=ControlAction(action="stop", reason="ok"),
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.05)
        assert fired == [1]
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_worker_failure_discards_callback() -> None:
    """The bug we're fixing: subprocess crash mid-turn must NOT mark the
    reminder fired. Discarding leaves the DB row pending so the next
    reminder loop tick re-fires it."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    fired: list[int] = []

    async def cb() -> None:
        fired.append(1)

    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1), on_success=cb)
        await asyncio.sleep(0.08)
        assert worker.sent

        worker.feed(RuntimeError("cc subprocess wedged"))
        await asyncio.sleep(0.05)
        assert fired == [], "callback fired despite worker failure"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_recoverable_dropped_text_holds_callback_until_retry() -> None:
    """Recoverable dropped-text: turn continues with an injected
    ``<error>``. Callback must wait for the retry's outcome, not fire
    on the first (failed) result."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    fired: list[int] = []

    async def cb() -> None:
        fired.append(1)

    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1), on_success=cb)
        await asyncio.sleep(0.08)

        worker.feed(
            TurnResult(
                text_blocks=["I would say hi"],
                control=None,
                dropped_text=True,
            )
        )
        await asyncio.sleep(0.05)
        assert fired == [], "callback fired during recoverable dropped_text"

        worker.feed(
            TurnResult(
                control=ControlAction(action="stop", reason="ok"),
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.05)
        assert fired == [1]
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_dropped_text_cap_hit_fires_callback() -> None:
    """Cap-hit ends the turn for good — CC saw the reminder and just
    can't form a valid response. Fire the callback so the reminder
    advances; otherwise the reminder loop would re-fire forever and
    repeatedly hit the same cap."""
    cfg = dataclasses.replace(Config.for_test(Path("/tmp")), tool_error_max_count=1)

    worker = FakeWorker()
    eng = Engine(worker, cfg, debounce_ms=20)
    fired: list[int] = []

    async def cb() -> None:
        fired.append(1)

    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1), on_success=cb)
        await asyncio.sleep(0.08)

        worker.feed(
            TurnResult(
                text_blocks=["nope"],
                control=None,
                dropped_text=True,
            )
        )
        await asyncio.sleep(0.05)
        assert fired == [1]
    finally:
        await eng.stop()
