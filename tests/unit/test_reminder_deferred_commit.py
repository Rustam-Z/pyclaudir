"""Engine-level on_success / on_failure contract behind reminder delivery
(issue #22).

The reminder loop hangs advance/close on ``on_success`` and revert on
``on_failure`` (see :meth:`Engine.submit`), so the claimed DB row is only
committed after CC actually consumes the turn. These tests pin the three
outcomes the fix depends on:

1. clean turn end → ``on_success`` fires, ``on_failure`` does not
2. CC subprocess crash → ``on_failure`` fires (revert the claim; retry next tick)
3. dropped-text → the engine delivers the text and fires ``on_success``
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hamroh.cc_worker import TurnResult
from hamroh.config import Config
from hamroh.engine import Engine, EngineOptions
from hamroh.models import ChatMessage, ControlAction

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


class _Hooks:
    """Records which of a submit's paired hooks fired."""

    def __init__(self) -> None:
        self.succeeded: list[int] = []
        self.reverted: list[int] = []

    async def on_success(self) -> None:
        self.succeeded.append(1)

    async def on_failure(self) -> None:
        self.reverted.append(1)


@pytest.mark.asyncio
async def test_on_success_fires_after_clean_turn_end() -> None:
    """Happy path: on_success runs once the turn ends with action=stop;
    on_failure is left untouched."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    hooks = _Hooks()

    await eng.start()
    try:
        await eng.submit(
            _msg("hi", mid=1), on_success=hooks.on_success, on_failure=hooks.on_failure
        )
        await asyncio.sleep(0.08)
        assert worker.sent, "turn did not start"
        assert hooks.succeeded == [], "on_success fired before the turn result"

        worker.feed(
            TurnResult(
                control=ControlAction(action="stop", reason="ok"),
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.05)
        assert hooks.succeeded == [1], "on_success must fire on a clean turn"
        assert hooks.reverted == [], "on_failure must not fire on a clean turn"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_worker_failure_fires_on_failure() -> None:
    """The bug we're fixing: a subprocess crash mid-turn must NOT mark the
    reminder delivered. on_failure fires so the claim is reverted and the
    next reminder loop tick re-fires it."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    hooks = _Hooks()

    await eng.start()
    try:
        await eng.submit(
            _msg("hi", mid=1), on_success=hooks.on_success, on_failure=hooks.on_failure
        )
        await asyncio.sleep(0.08)
        assert worker.sent

        worker.feed(RuntimeError("cc subprocess wedged"))
        await asyncio.sleep(0.05)
        assert hooks.succeeded == [], "on_success must not fire on a worker crash"
        assert hooks.reverted == [1], "on_failure must fire so the reminder reverts"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_dropped_text_delivers_and_fires_on_success() -> None:
    """Dropped text ends the turn immediately: the engine delivers the text
    it already produced and fires on_success, so a reminder that triggered
    the turn advances instead of re-firing. on_failure stays untouched."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    hooks = _Hooks()

    await eng.start()
    try:
        await eng.submit(
            _msg("hi", mid=1), on_success=hooks.on_success, on_failure=hooks.on_failure
        )
        await asyncio.sleep(0.08)

        worker.feed(
            TurnResult(
                text_blocks=["I would say hi"],
                control=ControlAction(action="stop", reason="answered"),
                dropped_text=True,
            )
        )
        await asyncio.sleep(0.05)
        assert hooks.succeeded == [1], "dropped-text turn must fire on_success once"
        assert hooks.reverted == [], "on_failure must not fire when text was delivered"
    finally:
        await eng.stop()
