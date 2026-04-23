"""Tool-error circuit breaker — aborts a stuck turn after N errors.

Feeds synthetic ``tool_result`` events with ``is_error=true`` into
``_handle_event`` and verifies the worker terminates the subprocess
and signals the engine via a sentinel :class:`TurnResult`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyclaudir.cc_worker import CcSpawnSpec, CcWorker, TurnResult


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


@pytest.fixture
def worker(tmp_path: Path) -> CcWorker:
    w = CcWorker(_spec(tmp_path))
    w._proc = MagicMock()
    w._proc.returncode = None
    w._current_turn = TurnResult()
    return w


@pytest.mark.asyncio
async def test_breaker_trips_on_third_error(worker: CcWorker) -> None:
    """Three tool errors in quick succession → abort + sentinel on queue."""

    terminate_called = asyncio.Event()

    async def fake_terminate() -> None:
        terminate_called.set()

    worker._terminate_proc = fake_terminate  # type: ignore[assignment]

    # First two errors: counter ticks up, no abort yet.
    worker._handle_event(_tool_error_event("toolu_1"))
    worker._handle_event(_tool_error_event("toolu_2"))
    assert worker._turn_tool_error_count == 2
    assert worker._result_queue.empty()

    # Third error: breaker trips.
    worker._handle_event(_tool_error_event("toolu_3"))

    # Give the scheduled terminate task a chance to run.
    await asyncio.wait_for(terminate_called.wait(), timeout=1.0)

    # Sentinel was delivered immediately, before terminate landed.
    result = worker._result_queue.get_nowait()
    assert isinstance(result, TurnResult)
    assert result.aborted_reason == "tool-error-limit"
    assert worker._last_abort_reason == "tool-error-limit"


@pytest.mark.asyncio
async def test_breaker_trips_on_window_expiry(worker: CcWorker) -> None:
    """One error outside the window of the first one → abort even
    though count < max."""
    import time

    os.environ["PYCLAUDIR_TOOL_ERROR_MAX_COUNT"] = "99"  # disable count trigger
    os.environ["PYCLAUDIR_TOOL_ERROR_WINDOW_SECONDS"] = "0.05"
    terminate_called = asyncio.Event()

    async def fake_terminate() -> None:
        terminate_called.set()

    worker._terminate_proc = fake_terminate  # type: ignore[assignment]

    try:
        # First error starts the window.
        worker._handle_event(_tool_error_event("toolu_1"))
        # Backdate the first-error timestamp past the window.
        worker._turn_first_tool_error_at = time.monotonic() - 1.0
        # Next error should trip the window check.
        worker._handle_event(_tool_error_event("toolu_2"))
        await asyncio.wait_for(terminate_called.wait(), timeout=1.0)
    finally:
        del os.environ["PYCLAUDIR_TOOL_ERROR_MAX_COUNT"]
        del os.environ["PYCLAUDIR_TOOL_ERROR_WINDOW_SECONDS"]

    result = worker._result_queue.get_nowait()
    assert result.aborted_reason == "tool-error-limit"


@pytest.mark.asyncio
async def test_successful_tool_does_not_trip_breaker(worker: CcWorker) -> None:
    """Mixed errors + successes: only is_error=true events count."""
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

    terminate_called = asyncio.Event()

    async def fake_terminate() -> None:
        terminate_called.set()

    worker._terminate_proc = fake_terminate  # type: ignore[assignment]

    for _ in range(5):
        worker._handle_event(ok_event)

    assert worker._turn_tool_error_count == 0
    assert not terminate_called.is_set()
    assert worker._result_queue.empty()


@pytest.mark.asyncio
async def test_send_resets_counters(worker: CcWorker) -> None:
    """A new turn (new ``send()``) resets the error count + window."""
    worker._handle_event(_tool_error_event("toolu_1"))
    worker._handle_event(_tool_error_event("toolu_2"))
    assert worker._turn_tool_error_count == 2

    # Stub the stdin write path so send() doesn't blow up on the MagicMock.
    worker._proc.stdin = MagicMock()
    worker._proc.stdin.write = MagicMock()

    async def fake_drain() -> None:
        return None

    worker._proc.stdin.drain = fake_drain  # type: ignore[assignment]

    await worker.send("next turn")
    assert worker._turn_tool_error_count == 0
    assert worker._turn_first_tool_error_at is None


def test_consume_abort_reason_is_one_shot(tmp_path: Path) -> None:
    w = CcWorker(_spec(tmp_path))
    w._last_abort_reason = "tool-error-limit"
    assert w.consume_abort_reason() == "tool-error-limit"
    assert w.consume_abort_reason() is None
