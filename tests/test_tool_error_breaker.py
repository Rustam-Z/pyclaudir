"""Tool-error circuit breaker — aborts a stuck turn on whichever
fires first: ``tool_error_max_count`` errors in the turn (count
branch), or ``tool_error_window`` seconds elapsed since the first
error (watchdog branch).

Feeds synthetic ``tool_result`` events with ``is_error=true`` into
``_handle_event`` and verifies the worker terminates the subprocess
and signals the engine via a sentinel :class:`TurnResult`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyclaudir.cc_worker import CcSpawnSpec, CcWorker, TurnResult
from pyclaudir.config import Config


def _spec(tmp_path: Path) -> CcSpawnSpec:
    sp = tmp_path / "system.md"
    sp.write_text("system")
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {}}')
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    return CcSpawnSpec(
        binary="/bin/true",
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
    )


def _tool_error_event(uid: str = "toolu_1") -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": uid,
                    "content": "Permission denied",
                    "is_error": True,
                }
            ],
        },
    }


def _result_event() -> dict:
    return {"type": "result", "result": {"action": "stop", "reason": "done"}}


@pytest.fixture
def worker(tmp_path: Path) -> CcWorker:
    w = CcWorker(_spec(tmp_path), Config.for_test(tmp_path))
    w._proc = MagicMock()
    w._proc.returncode = None
    w._current_turn = TurnResult()
    return w


@pytest.fixture
def fast_window_worker(worker: CcWorker) -> CcWorker:
    """Worker with a 50ms watchdog window for fast async tests."""
    worker._tool_error_window = 0.05
    return worker


def _attach_terminate_event(worker: CcWorker) -> asyncio.Event:
    """Stub ``_terminate_proc`` so it just signals an event."""
    terminate_called = asyncio.Event()

    async def fake_terminate() -> None:
        terminate_called.set()

    worker._terminate_proc = fake_terminate  # type: ignore[assignment]
    return terminate_called


@pytest.mark.asyncio
async def test_count_branch_trips_on_third_error(worker: CcWorker) -> None:
    """Count branch: three tool errors in quick succession → abort +
    sentinel on queue. Watchdog gets cancelled."""
    terminate_called = _attach_terminate_event(worker)

    # First two errors: count ticks up, watchdog scheduled, no abort yet.
    worker._handle_event(_tool_error_event("toolu_1"))
    worker._handle_event(_tool_error_event("toolu_2"))
    assert worker._turn_tool_error_count == 2
    assert worker._tool_error_watchdog_task is not None
    assert not worker._tool_error_watchdog_task.done()
    assert worker._result_queue.empty()

    # Third error: breaker trips on count branch.
    worker._handle_event(_tool_error_event("toolu_3"))

    await asyncio.wait_for(terminate_called.wait(), timeout=1.0)

    # Watchdog was cancelled when the count branch tripped.
    assert worker._tool_error_watchdog_task is None

    # Sentinel was delivered immediately, before terminate landed.
    result = worker._result_queue.get_nowait()
    assert isinstance(result, TurnResult)
    assert result.aborted_reason == "tool-error-limit"


@pytest.mark.asyncio
async def test_watchdog_fires_alone_with_single_error(
    fast_window_worker: CcWorker,
) -> None:
    """Watchdog branch: a single error followed by silence still trips
    the breaker once the window expires. This is the headline case the
    user asked about — 'if 1 error in 30 seconds, still report'."""
    worker = fast_window_worker
    terminate_called = _attach_terminate_event(worker)

    worker._handle_event(_tool_error_event("toolu_1"))
    assert worker._turn_tool_error_count == 1
    assert worker._tool_error_watchdog_task is not None

    # No further errors. Watchdog should fire on its own ~50ms later.
    await asyncio.wait_for(terminate_called.wait(), timeout=1.0)

    result = worker._result_queue.get_nowait()
    assert result.aborted_reason == "tool-error-limit"


@pytest.mark.asyncio
async def test_watchdog_fires_with_two_errors_below_threshold(
    fast_window_worker: CcWorker,
) -> None:
    """Two errors within the window but below the count threshold: the
    watchdog still fires once the deadline lapses."""
    worker = fast_window_worker
    terminate_called = _attach_terminate_event(worker)

    worker._handle_event(_tool_error_event("toolu_1"))
    await asyncio.sleep(0.01)
    worker._handle_event(_tool_error_event("toolu_2"))
    assert worker._turn_tool_error_count == 2
    assert worker._tool_error_abort_task is None  # count branch hasn't tripped

    await asyncio.wait_for(terminate_called.wait(), timeout=1.0)

    result = worker._result_queue.get_nowait()
    assert result.aborted_reason == "tool-error-limit"


@pytest.mark.asyncio
async def test_successful_turn_cancels_watchdog(
    fast_window_worker: CcWorker,
) -> None:
    """A clean turn end (``result`` event) defuses the watchdog so a
    stale deadline can't trip the breaker after the fact."""
    worker = fast_window_worker
    terminate_called = _attach_terminate_event(worker)

    worker._handle_event(_tool_error_event("toolu_1"))
    watchdog = worker._tool_error_watchdog_task
    assert watchdog is not None
    assert not watchdog.done()

    # Turn finishes cleanly before the watchdog fires.
    worker._handle_event(_result_event())
    assert worker._tool_error_watchdog_task is None
    # The watchdog task should be cancelled. Allow the cancellation to
    # propagate (one event-loop tick).
    await asyncio.sleep(0)
    assert watchdog.cancelled() or watchdog.done()

    # Wait past the original deadline; nothing should fire.
    await asyncio.sleep(0.1)
    assert not terminate_called.is_set()
    # The result was queued, but it's the legitimate turn result, not
    # the breaker sentinel.
    queued = worker._result_queue.get_nowait()
    assert queued.aborted_reason is None
    assert queued.control is not None


@pytest.mark.asyncio
async def test_idempotent_trip_no_double_terminate(
    fast_window_worker: CcWorker,
) -> None:
    """Count branch trips first; if the watchdog had a chance to wake
    up later, ``_trip_tool_error_breaker`` is a no-op (abort task
    already in flight)."""
    worker = fast_window_worker
    terminate_calls = 0

    async def counting_terminate() -> None:
        nonlocal terminate_calls
        terminate_calls += 1

    worker._terminate_proc = counting_terminate  # type: ignore[assignment]

    # Three errors in quick succession — count branch trips.
    worker._handle_event(_tool_error_event("toolu_1"))
    worker._handle_event(_tool_error_event("toolu_2"))
    worker._handle_event(_tool_error_event("toolu_3"))

    # Wait past the original watchdog deadline AND let the abort task
    # complete. With window=50ms and a single asyncio.sleep(0.1), any
    # surviving watchdog would have fired by now.
    await asyncio.sleep(0.1)

    # Manual idempotency probe: directly invoke the trip helper as if
    # the watchdog had survived. Should be a no-op.
    worker._trip_tool_error_breaker(reason="window")

    assert terminate_calls == 1, (
        f"expected exactly one _terminate_proc call, got {terminate_calls}"
    )


@pytest.mark.asyncio
async def test_successful_tool_does_not_trip_breaker(worker: CcWorker) -> None:
    """Mixed successes only: no errors recorded, no watchdog scheduled,
    no trip."""
    ok_event = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_ok",
                    "content": "ok",
                    "is_error": False,
                }
            ],
        },
    }

    terminate_called = _attach_terminate_event(worker)

    for _ in range(5):
        worker._handle_event(ok_event)

    assert worker._turn_tool_error_count == 0
    assert worker._tool_error_watchdog_task is None
    assert not terminate_called.is_set()
    assert worker._result_queue.empty()


@pytest.mark.asyncio
async def test_send_resets_counters_and_cancels_watchdog(worker: CcWorker) -> None:
    """A new turn (new ``send()``) clears per-turn breaker state — count,
    first-error timestamp, and the wall-clock watchdog."""
    worker._handle_event(_tool_error_event("toolu_1"))
    worker._handle_event(_tool_error_event("toolu_2"))
    assert worker._turn_tool_error_count == 2
    watchdog = worker._tool_error_watchdog_task
    assert watchdog is not None and not watchdog.done()

    # Stub the stdin write path so send() doesn't blow up on the MagicMock.
    worker._proc.stdin = MagicMock()
    worker._proc.stdin.write = MagicMock()

    async def fake_drain() -> None:
        return None

    worker._proc.stdin.drain = fake_drain  # type: ignore[assignment]

    await worker.send("next turn")
    assert worker._turn_tool_error_count == 0
    assert worker._turn_first_tool_error_at is None
    assert worker._tool_error_watchdog_task is None
    await asyncio.sleep(0)  # let cancellation propagate
    assert watchdog.cancelled() or watchdog.done()


