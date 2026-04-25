"""Liveness monitor — detects wedged-mid-turn subprocesses.

We don't spawn a real subprocess; instead we fake just enough of the
:class:`CcWorker` surface the liveness loop reads.
"""

from __future__ import annotations

import asyncio
import time
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
    schema.write_text('{}')
    return CcSpawnSpec(
        binary="/bin/true",  # we never actually spawn
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
    )


@pytest.mark.asyncio
async def test_liveness_does_nothing_when_idle(tmp_path: Path) -> None:
    """No current turn = silence is expected, don't kill."""
    worker = CcWorker(_spec(tmp_path), Config.for_test(tmp_path))
    # Simulate running subprocess with no turn in flight.
    worker._proc = MagicMock()
    worker._proc.returncode = None  # running
    worker._current_turn = None

    # Make last_event_at ancient — but since we're idle, don't care.
    worker._last_event_at = time.monotonic() - 9999
    worker.heartbeat._last = time.monotonic() - 9999

    # Run the liveness check for a short cycle with a very short timeout.
    worker._liveness_poll = 0.05
    task = asyncio.create_task(worker._liveness_loop())
    await asyncio.sleep(0.15)  # a couple of poll cycles
    worker._stop_supervisor.set()
    await task

    # Proc was not terminated (we mocked it — just verify terminate wasn't called).
    # Since _terminate_proc does real work, the MagicMock for _proc wouldn't be
    # torn down by it; easier: verify the worker still has its _proc ref.
    assert worker._proc is not None


@pytest.mark.asyncio
async def test_liveness_kills_on_wedged_turn(tmp_path: Path) -> None:
    """Mid-turn, no activity past timeout → terminate."""
    worker = CcWorker(_spec(tmp_path), Config.for_test(tmp_path))
    worker._proc = MagicMock()
    worker._proc.returncode = None

    # Mid-turn.
    worker._current_turn = TurnResult()

    # No activity in a long time.
    worker._last_event_at = time.monotonic() - 9999
    worker.heartbeat._last = time.monotonic() - 9999

    # Fast poll + tiny timeout for the test.
    worker._liveness_poll = 0.05

    terminate_called = asyncio.Event()

    async def fake_terminate() -> None:
        terminate_called.set()

    worker._terminate_proc = fake_terminate  # type: ignore[assignment]

    # Override the resolved liveness timeout directly on the worker.
    worker._liveness_timeout = 0.01

    task = asyncio.create_task(worker._liveness_loop())
    await asyncio.wait_for(terminate_called.wait(), timeout=1.0)
    worker._stop_supervisor.set()
    await task

    assert terminate_called.is_set()


@pytest.mark.asyncio
async def test_liveness_resets_on_activity(tmp_path: Path) -> None:
    """Mid-turn but with recent activity → don't kill.

    Uses a generous timeout (1s) and a short test window (0.2s), so
    even though the monitor runs multiple poll cycles, activity stays
    'recent' the whole time.
    """
    worker = CcWorker(_spec(tmp_path), Config.for_test(tmp_path))
    worker._proc = MagicMock()
    worker._proc.returncode = None
    worker._current_turn = TurnResult()

    # Recent activity.
    worker._last_event_at = time.monotonic()
    worker.heartbeat._last = time.monotonic()

    worker._liveness_poll = 0.05

    terminate_called = asyncio.Event()

    async def fake_terminate() -> None:
        terminate_called.set()

    worker._terminate_proc = fake_terminate  # type: ignore[assignment]

    worker._liveness_timeout = 1.0

    task = asyncio.create_task(worker._liveness_loop())
    # Short window — well under the 1s timeout. Liveness should NOT fire.
    await asyncio.sleep(0.2)
    worker._stop_supervisor.set()
    await task

    assert not terminate_called.is_set()


@pytest.mark.asyncio
async def test_liveness_skips_when_not_running(tmp_path: Path) -> None:
    """No process = no liveness concern."""
    worker = CcWorker(_spec(tmp_path), Config.for_test(tmp_path))
    # _proc None = is_running False
    worker._current_turn = TurnResult()
    worker._last_event_at = time.monotonic() - 9999

    worker._liveness_poll = 0.05

    terminate_called = asyncio.Event()

    async def fake_terminate() -> None:
        terminate_called.set()

    worker._terminate_proc = fake_terminate  # type: ignore[assignment]

    task = asyncio.create_task(worker._liveness_loop())
    await asyncio.sleep(0.2)
    worker._stop_supervisor.set()
    await task

    assert not terminate_called.is_set()


def test_last_event_at_is_initialized() -> None:
    """Ensure the field exists and is initialized — avoids the
    _wire_handlers-style regression where the monitor reads an
    attribute that doesn't exist."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        worker = CcWorker(_spec(td_path), Config.for_test(td_path))
        assert hasattr(worker, "_last_event_at")
        assert isinstance(worker._last_event_at, float)
