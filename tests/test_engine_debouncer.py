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
async def test_typing_indicator_fires_when_model_engages() -> None:
    """Typing fires for every chat in the batch the moment the model
    signals engagement (first message-producing tool_use). In production
    the CC worker calls ``notify_model_engaged`` from its stdout
    reader; here we call it directly to simulate that signal."""
    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.1)  # let debounce fire and turn start
        # Pre-engagement: no typing yet (this is the new behavior).
        assert typing_calls == []
        # Model emits its first message-producing tool_use → typing fires.
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
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
        # Simulate the model engaging so typing actually fires.
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
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
    """If a single batch spans two chats, both get typing once the
    model engages."""
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
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
        assert -100 in typing_calls
        assert -200 in typing_calls
    finally:
        await eng.stop()


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
async def test_notify_chat_replied_stops_typing_after_min_visible_duration() -> None:
    """``notify_chat_replied`` should defer the actual stop until typing
    has been visible for at least ``MIN_TYPING_VISIBLE_SECONDS`` so the
    user's Telegram client actually renders the indicator.

    Without this defer, a fast turn 2 (warm CC, response in <1s) would
    have its typing call dismissed before the client could render it.
    """
    from pyclaudir.engine import MIN_TYPING_VISIBLE_SECONDS

    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.1)
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
        assert len(typing_calls) >= 1

        # Send_message lands FAST (well under MIN_TYPING_VISIBLE_SECONDS)
        eng.notify_chat_replied(-100)

        # The typing chat should NOT be cleared yet — the deferral is in
        # flight, and the loop is still alive.
        await asyncio.sleep(0.05)
        assert -100 in eng._typing_chats, (
            "notify_chat_replied stopped typing too early; user wouldn't "
            "see the indicator"
        )
        assert eng._typing_task is not None and not eng._typing_task.done()

        # After the min duration elapses, the deferred stop should fire
        # and the loop should exit on its next tick.
        await asyncio.sleep(MIN_TYPING_VISIBLE_SECONDS + 0.1)
        assert -100 not in eng._typing_chats
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
        await asyncio.sleep(0.05)
        eng.notify_model_engaged()
        # Wait long enough that the min visible duration has passed
        await asyncio.sleep(MIN_TYPING_VISIBLE_SECONDS + 0.1)
        assert len(typing_calls) >= 1

        # NOW notify — should stop immediately, no defer
        eng.notify_chat_replied(-100)
        await asyncio.sleep(0.05)
        assert -100 not in eng._typing_chats
        # The deferred-stop task should NOT have been spawned
        assert eng._typing_deferred_stop is None
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
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
        assert -100 in typing_calls
        assert -200 in typing_calls

        # Reply only to group A
        eng.notify_chat_replied(-100)
        await asyncio.sleep(0.05)
        # Group B is still in the typing set
        assert -200 in eng._typing_chats
        # Loop is still alive
        assert eng._typing_task is not None and not eng._typing_task.done()
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
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
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
        assert eng._typing_task is None or eng._typing_task.done()

        # === turn 2 — the regression case ===
        await eng.submit(_msg("second", mid=2))
        await asyncio.sleep(0.1)
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)

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
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
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
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
        assert len(typing_calls) >= 1
        calls_after_initial = len(typing_calls)

        # send_message lands → notify_chat_replied stops typing
        # (slow turn so MIN_TYPING_VISIBLE_SECONDS doesn't kick in)
        from pyclaudir.engine import MIN_TYPING_VISIBLE_SECONDS

        await asyncio.sleep(MIN_TYPING_VISIBLE_SECONDS + 0.1)
        eng.notify_chat_replied(-100)
        await asyncio.sleep(0.05)

        # Typing task should have exited
        assert eng._typing_task is None or eng._typing_task.done()
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
        assert eng._typing_task is not None and not eng._typing_task.done()
    finally:
        await eng.stop()


# Removed: ``test_typing_completes_before_cc_send``. It enforced
# typing-fires-before-worker.send to dodge a PTB connection-pool race.
# That ordering no longer applies — typing now fires *after* worker.send,
# specifically when the worker reports the model's first message-producing
# tool_use. The race the test guarded against is gone.


@pytest.mark.asyncio
async def test_typing_does_not_fire_without_engagement() -> None:
    """The headline UX fix: a turn that ends in StructuredOutput stop with
    no message-producing tool_use must NEVER show typing. Otherwise the
    user sees ``Nodira is typing… [silence]`` for messages the bot
    decided to ignore (group peer chatter, m1470-style quote echoes).
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
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.1)  # debounce + turn kick

        # Worker reports a clean stop with no tool_use blocks.
        # Equivalent: model decided to stay silent.
        worker.feed_result(TurnResult(
            text_blocks=[],
            control=ControlAction(action="stop", reason="no reply needed"),
            dropped_text=False,
        ))
        await asyncio.sleep(0.1)

        # Typing was NEVER fired.
        assert typing_calls == [], (
            f"typing fired without engagement signal: {typing_calls}"
        )
        assert eng._typing_chats == set()
        assert eng._typing_task is None or eng._typing_task.done()
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_typing_fires_on_first_message_tool_use() -> None:
    """The other half of the contract: when the model emits a
    message-producing tool_use, ``notify_model_engaged`` is the engine
    entry point that fires typing and starts the refresh loop.
    Idempotent — a second call mid-turn is a no-op for already-firing
    chats.
    """
    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.1)
        # Pre-engagement: silent.
        assert typing_calls == []
        assert eng._typing_task is None or eng._typing_task.done()

        # Model engages (worker would call this from its stdout reader).
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
        assert -100 in typing_calls
        assert -100 in eng._typing_chats
        assert eng._typing_task is not None and not eng._typing_task.done()

        # Idempotent — a second engaged signal in the same turn doesn't
        # fire a duplicate typing call for an already-covered chat.
        before = len(typing_calls)
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
        # The refresh loop may have ticked once during the sleep, so
        # we accept "no extra call beyond a refresh tick"; what we
        # really care about is no *immediate* extra fire.
        assert len(typing_calls) - before <= 1
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_notify_model_engaged_no_active_chats_is_noop() -> None:
    """If the engine isn't mid-turn, the worker shouldn't be calling
    ``notify_model_engaged`` — but be defensive: it must not crash and
    must not fire typing for whatever chat happened to be in
    ``_typing_chats`` from a previous turn."""
    worker = FakeWorker()
    typing_calls: list[int] = []

    async def fake_typing(chat_id: int) -> None:
        typing_calls.append(chat_id)

    eng = Engine(worker, _CFG, debounce_ms=20, typing_action=fake_typing)
    await eng.start()
    try:
        # No submit, no active chats.
        eng.notify_model_engaged()
        await asyncio.sleep(0.05)
        assert typing_calls == []
    finally:
        await eng.stop()
