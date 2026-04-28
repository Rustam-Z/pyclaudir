"""Engine behaviour: XML formatting and the debounce/process flag."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from pathlib import Path

from pyclaudir.config import Config
from pyclaudir.engine import Engine, format_messages_as_xml
from pyclaudir.models import ChatMessage


#: Test Config constant. Engine doesn't touch the filesystem (the worker
#: does), so a placeholder ``data_dir`` is fine. Per-test knob overrides
#: go on the engine instance (``eng._progress_notify_seconds = 0.05``).
_CFG = Config.for_test(Path("/tmp"))


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
    eng = Engine(worker, _CFG, debounce_ms=50)
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
    eng = Engine(worker, _CFG, debounce_ms=50)
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


@pytest.mark.asyncio
async def test_typing_indicator_fires_on_turn_start() -> None:
    """The engine should call the typing_action callback for every chat
    in the batch as soon as a turn begins."""
    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.1)  # let debounce fire and turn start
        # Typing fired at least once for chat -100
        assert -100 in typing_calls
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_typing_indicator_stops_when_turn_ends() -> None:
    """Once the turn finishes, the typing refresh loop should stop firing."""
    from pyclaudir.cc_worker import TurnResult
    from pyclaudir.models import ControlAction

    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.1)
        initial = len(typing_calls)
        assert initial >= 1

        # Finish the turn cleanly
        worker.feed_result(TurnResult(
            text_blocks=[],
            control=ControlAction(action="stop", reason="ok"),
            dropped_text=False,
        ))
        await asyncio.sleep(0.1)  # control loop processes result

        # No more typing calls after the turn ends — wait long enough that
        # a refresh tick *would* have fired if the loop were still alive.
        before_wait = len(typing_calls)
        await asyncio.sleep(0.1)
        assert len(typing_calls) == before_wait, (
            "typing kept firing after turn ended"
        )
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_typing_fires_for_every_chat_in_a_multi_chat_batch() -> None:
    """If a single batch spans two chats, both get typing."""
    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        # Two messages from different chats arrive within the debounce window
        m_a = ChatMessage(
            chat_id=-100, message_id=1, user_id=42, direction="in",
            timestamp=datetime(2026, 4, 12, 10, 0, tzinfo=timezone.utc),
            text="from group A",
        )
        m_b = ChatMessage(
            chat_id=-200, message_id=1, user_id=43, direction="in",
            timestamp=datetime(2026, 4, 12, 10, 0, tzinfo=timezone.utc),
            text="from group B",
        )
        await eng.submit(m_a)
        await eng.submit(m_b)
        await asyncio.sleep(0.1)
        assert -100 in typing_calls
        assert -200 in typing_calls
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_is_busy_false_for_fresh_engine() -> None:
    """No turn started, no user activity → not busy."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    assert eng.is_busy() is False


@pytest.mark.asyncio
async def test_is_busy_true_during_turn() -> None:
    """Engine processing a turn → busy."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=10)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.05)  # let debounce + kick fire
        assert eng.is_busy() is True
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_is_busy_true_after_recent_user_inbound() -> None:
    """User just messaged, turn finished → still busy until quiet window."""
    import pyclaudir.engine as engine_mod

    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=10)
    # submit a real user message (mid=1) without starting the engine, so
    # is_processing stays clear; we're only checking the recency clause.
    await eng.submit(_msg("hi", mid=1))
    # Drain pending so we can isolate the recency check from "_pending non-empty".
    eng._pending.clear()
    assert eng.is_busy() is True

    # Pretend the recency window has elapsed.
    import time
    eng._last_user_inbound_at = time.monotonic() - engine_mod.REMINDER_QUIET_SECONDS - 1
    assert eng.is_busy() is False


@pytest.mark.asyncio
async def test_is_busy_ignores_synthetic_reminder_inbound() -> None:
    """A reminder inject (mid=0) must NOT update the user-activity clock —
    otherwise reminders would defer themselves forever."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=10)
    await eng.submit(_msg("<reminder>...</reminder>", mid=0))
    eng._pending.clear()  # isolate from the pending check
    assert eng._last_user_inbound_at == 0.0
    assert eng.is_busy() is False


@pytest.mark.asyncio
async def test_no_typing_action_is_safe() -> None:
    """Engine without typing_action should still work — old default."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=None)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.1)
        assert len(worker.sent) == 1
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_notify_chat_replied_stops_typing_after_min_visible_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``notify_chat_replied`` should defer the actual stop until typing
    has been visible for at least ``MIN_TYPING_VISIBLE_SECONDS`` so the
    user's Telegram client actually renders the indicator.

    Without this defer, a fast turn 2 (warm CC, response in <1s) would
    have its typing call dismissed before the client could render it.

    The prod default is currently 0 (deferral disabled); we override it
    here so the deferral branch is still exercised.
    """
    import pyclaudir.engine as engine_mod

    monkeypatch.setattr(engine_mod, "MIN_TYPING_VISIBLE_SECONDS", 1)
    MIN_TYPING_VISIBLE_SECONDS = engine_mod.MIN_TYPING_VISIBLE_SECONDS

    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.1)
        assert len(typing_calls) >= 1

        # Send_message lands FAST (well under MIN_TYPING_VISIBLE_SECONDS)
        eng.notify_chat_replied(-100)

        # The typing chat should NOT be cleared yet — the deferral is in
        # flight, and the loop is still alive.
        await asyncio.sleep(0.05)
        assert -100 in eng._typing.chats, (
            "notify_chat_replied stopped typing too early; user wouldn't "
            "see the indicator"
        )
        assert eng._typing.task is not None and not eng._typing.task.done()

        # After the min duration elapses, the deferred stop should fire
        # and the loop should exit on its next tick.
        await asyncio.sleep(MIN_TYPING_VISIBLE_SECONDS + 0.1)
        assert -100 not in eng._typing.chats
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_notify_chat_replied_stops_immediately_when_already_visible_long_enough() -> None:
    """If typing has already been visible for ``MIN_TYPING_VISIBLE_SECONDS``,
    the stop happens immediately. Used by slow turns where the indicator
    has been on screen for several seconds already."""
    from pyclaudir.engine import MIN_TYPING_VISIBLE_SECONDS

    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        # Wait long enough that the min visible duration has passed
        await asyncio.sleep(MIN_TYPING_VISIBLE_SECONDS + 0.1)
        assert len(typing_calls) >= 1

        # NOW notify — should stop immediately, no defer
        eng.notify_chat_replied(-100)
        await asyncio.sleep(0.05)
        assert -100 not in eng._typing.chats
        # The deferred-stop task should NOT have been spawned
        assert eng._typing.deferred_stop is None
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_notify_chat_replied_only_stops_the_named_chat() -> None:
    """Multi-chat batch: replying to one chat must NOT stop typing in
    the other one — that one is still waiting."""
    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        m_a = ChatMessage(
            chat_id=-100, message_id=1, user_id=42, direction="in",
            timestamp=datetime(2026, 4, 12, 10, 0, tzinfo=timezone.utc),
            text="from group A",
        )
        m_b = ChatMessage(
            chat_id=-200, message_id=1, user_id=43, direction="in",
            timestamp=datetime(2026, 4, 12, 10, 0, tzinfo=timezone.utc),
            text="from group B",
        )
        await eng.submit(m_a)
        await eng.submit(m_b)
        await asyncio.sleep(0.1)
        assert -100 in typing_calls
        assert -200 in typing_calls

        # Reply only to group A
        eng.notify_chat_replied(-100)
        await asyncio.sleep(0.05)
        # Group B is still in the typing set
        assert -200 in eng._typing.chats
        # Loop is still alive
        assert eng._typing.task is not None and not eng._typing.task.done()
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_typing_fires_on_two_consecutive_turns() -> None:
    """Regression: 'typing only visible on the first message after start.'

    Run two complete turns end-to-end (kick → send_message → result → kick
    again) and assert that the typing_action was called for BOTH turns,
    not just the first.
    """
    from pyclaudir.cc_worker import TurnResult
    from pyclaudir.models import ControlAction

    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        # === turn 1 ===
        await eng.submit(_msg("first", mid=1))
        await asyncio.sleep(0.1)
        assert len(typing_calls) >= 1, "no typing on turn 1"
        turn1_calls = len(typing_calls)

        # send_message lands → notify
        eng.notify_chat_replied(-100)
        await asyncio.sleep(0.05)

        # CC eventually emits the result
        worker.feed_result(TurnResult(
            text_blocks=[],
            control=ControlAction(action="stop", reason="ok"),
            dropped_text=False,
        ))
        await asyncio.sleep(0.1)

        # Engine should be idle now
        assert not eng._is_processing.is_set()
        assert eng._typing.task is None or eng._typing.task.done()

        # === turn 2 — the regression case ===
        await eng.submit(_msg("second", mid=2))
        await asyncio.sleep(0.1)

        # The new typing call MUST have fired
        turn2_new_calls = len(typing_calls) - turn1_calls
        assert turn2_new_calls >= 1, (
            f"no typing on turn 2 — only {turn1_calls} calls total, "
            f"all from turn 1. typing_calls={typing_calls}"
        )

        # And complete turn 2 cleanly
        eng.notify_chat_replied(-100)
        worker.feed_result(TurnResult(
            text_blocks=[],
            control=ControlAction(action="stop", reason="ok"),
            dropped_text=False,
        ))
        await asyncio.sleep(0.1)

        # === turn 3 just to be paranoid ===
        before_3 = len(typing_calls)
        await eng.submit(_msg("third", mid=3))
        await asyncio.sleep(0.1)
        assert len(typing_calls) - before_3 >= 1, "no typing on turn 3"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_inject_after_notify_restarts_typing() -> None:
    """Regression: 'no typing on the second message in a turn'.

    Sequence:
    1. User sends msg 1, turn 1 begins, typing on
    2. Model calls send_message → notify_chat_replied stops typing
    3. CC keeps processing (StructuredOutput etc.) — turn not yet "done"
    4. User sends msg 2 BEFORE the result event lands
    5. Engine injects msg 2 into the running turn
    6. Typing must restart for the inject — the user is waiting again

    Before the fix: step 6 didn't happen (typing task was already done,
    so the "add to set" path was a no-op). User saw no typing while CC
    processed the injected message.
    """
    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        # === turn 1 ===
        await eng.submit(_msg("first", mid=1))
        await asyncio.sleep(0.1)
        assert len(typing_calls) >= 1

        # send_message lands → notify_chat_replied stops typing
        # (slow turn so MIN_TYPING_VISIBLE_SECONDS doesn't kick in)
        from pyclaudir.engine import MIN_TYPING_VISIBLE_SECONDS

        await asyncio.sleep(MIN_TYPING_VISIBLE_SECONDS + 0.1)
        eng.notify_chat_replied(-100)
        await asyncio.sleep(0.05)

        # Typing task should have exited
        assert eng._typing.task is None or eng._typing.task.done()
        calls_after_notify = len(typing_calls)

        # === inject — second message arrives MID-TURN, before result ===
        await eng.submit(_msg("second", mid=2))
        await asyncio.sleep(0.1)

        # The inject path must have restarted typing
        new_calls = len(typing_calls) - calls_after_notify
        assert new_calls >= 1, (
            f"no typing call fired for the injected message; "
            f"calls={typing_calls}"
        )
        # And the typing task is alive again
        assert eng._typing.task is not None and not eng._typing.task.done()
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_typing_completes_before_cc_send() -> None:
    """The first typing call must complete BEFORE the user envelope reaches
    the CC subprocess. Otherwise PTB's single connection pool can let the
    later send_message HTTP POST race ahead of the typing POST, and the
    user never sees typing because Telegram got the message first.

    This is the deliberate trade we make: ~100-300ms of added latency once
    per turn, in exchange for reliable typing visibility.
    """
    worker = FakeWorker()
    typing_completed = asyncio.Event()
    send_called_at: list[float] = []
    typing_completed_at: list[float] = []

    import time

    async def slow_typing(chat_id: int) -> None:
        await asyncio.sleep(0.05)  # simulate 50ms HTTP round trip
        typing_completed_at.append(time.monotonic())
        typing_completed.set()

    # Wrap the worker's send to record its timing
    original_send = worker.send

    async def timed_send(text: str) -> None:
        send_called_at.append(time.monotonic())
        await original_send(text)

    worker.send = timed_send  # type: ignore[method-assign]

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=slow_typing)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.wait_for(typing_completed.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        # Both should have happened
        assert len(typing_completed_at) == 1
        assert len(send_called_at) == 1
        # Critical: typing finished BEFORE worker.send was called
        assert typing_completed_at[0] <= send_called_at[0], (
            f"worker.send fired before typing completed: "
            f"typing={typing_completed_at[0]}, send={send_called_at[0]}"
        )
    finally:
        await eng.stop()
