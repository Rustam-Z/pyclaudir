"""Engine behaviour: XML formatting and the debounce/process flag."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from pyclaudir.engine import Engine, format_messages_as_xml
from pyclaudir.models import ChatMessage


def _msg(text: str, mid: int = 1) -> ChatMessage:
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


def test_xml_format_basic() -> None:
    out = format_messages_as_xml([_msg("hello")])
    assert "<msg" in out
    assert "hello" in out
    assert 'id="1"' in out
    assert 'chat="-100"' in out
    assert 'user="42"' in out
    assert 'time="10:31"' in out


def test_xml_format_escapes_html() -> None:
    out = format_messages_as_xml([_msg("<script>alert(1)</script>")])
    assert "&lt;script&gt;" in out
    assert "<script>alert" not in out


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


@pytest.mark.asyncio
async def test_debouncer_coalesces_burst() -> None:
    worker = FakeWorker()
    eng = Engine(worker, debounce_ms=50)
    await eng.start()
    try:
        for i in range(5):
            await eng.submit(_msg(f"m{i}", mid=i))
            await asyncio.sleep(0.01)  # < debounce
        # Wait long enough for the debounce timer to fire.
        await asyncio.sleep(0.15)
        assert len(worker.sent) == 1, f"expected one batched send, got {len(worker.sent)}"
        # All five messages should be in the single XML payload.
        for i in range(5):
            assert f"m{i}" in worker.sent[0]
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_inject_used_when_processing() -> None:
    worker = FakeWorker()
    eng = Engine(worker, debounce_ms=50)
    await eng.start()
    try:
        await eng.submit(_msg("first", mid=1))
        await asyncio.sleep(0.1)  # let debounce fire and start the turn
        assert len(worker.sent) == 1
        # New message arrives while turn is in progress.
        await eng.submit(_msg("mid-turn", mid=2))
        await asyncio.sleep(0.05)
        assert len(worker.injected) == 1
        assert "mid-turn" in worker.injected[0]
    finally:
        await eng.stop()
