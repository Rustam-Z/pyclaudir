"""Per-turn status heartbeat — while a turn keeps running, report progress to
the owner every ``status_interval_seconds`` instead of going silent.

Worker-side (not the agent), so it fires even if the agent is wedged. It does
NOT stop the turn — a long task keeps going; the owner replies "stop" to halt
it. The heartbeat carries the agent's last tool action so the ping is useful.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hamroh.cc_worker import CcSpawnSpec, CcWorker, TurnResult
from hamroh.config import Config


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


def _tool_use_event(name: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": name, "id": "t1", "input": {}}],
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
    w._status_interval = 0.05  # fast for tests
    return w


@pytest.mark.asyncio
async def test_heartbeat_reports_progress_without_stopping(worker: CcWorker) -> None:
    """The headline behaviour: it pings with elapsed + last action, and the turn
    keeps running (no abort, nothing on the result queue)."""
    calls: list[tuple[float, str | None]] = []

    async def on_status(elapsed: float, last_action: str | None) -> None:
        calls.append((elapsed, last_action))

    worker._on_status = on_status
    worker._last_tool_action = "browser_navigate"
    worker._arm_status_heartbeat()

    await asyncio.sleep(0.12)  # ~2 intervals
    worker._cancel_status_heartbeat()

    assert len(calls) >= 1, "must report progress while the turn runs"
    elapsed, action = calls[0]
    assert action == "browser_navigate", "the ping must name the last action"
    assert elapsed > 0, "elapsed time must be reported"
    assert worker._current_turn is not None, "the turn must NOT be stopped"
    assert worker._result_queue.empty(), "the heartbeat must not end the turn"


@pytest.mark.asyncio
async def test_heartbeat_recurs(worker: CcWorker) -> None:
    """It keeps pinging every interval, not just once."""
    calls: list[int] = []

    async def on_status(elapsed: float, last_action: str | None) -> None:
        calls.append(1)

    worker._on_status = on_status
    worker._arm_status_heartbeat()
    await asyncio.sleep(0.17)  # ~3 intervals
    worker._cancel_status_heartbeat()

    assert len(calls) >= 2, "the heartbeat must recur, not fire once"


@pytest.mark.asyncio
async def test_clean_result_stops_the_heartbeat(worker: CcWorker) -> None:
    """A finished turn stops the pings."""
    calls: list[int] = []

    async def on_status(elapsed: float, last_action: str | None) -> None:
        calls.append(1)

    worker._on_status = on_status
    worker._arm_status_heartbeat()
    task = worker._status_task
    assert task is not None

    worker._handle_event(_result_event())
    assert worker._status_task is None, "clean result must cancel the heartbeat"
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()

    await asyncio.sleep(0.12)
    assert calls == [], "no status pings after the turn ended"


@pytest.mark.asyncio
async def test_send_rearms_the_heartbeat(worker: CcWorker) -> None:
    """Each new turn restarts the heartbeat and clears the last action."""
    worker._last_tool_action = "browser_click"
    worker._arm_status_heartbeat()
    first = worker._status_task

    worker._proc.stdin = MagicMock()
    worker._proc.stdin.write = MagicMock()

    async def fake_drain() -> None:
        return None

    worker._proc.stdin.drain = fake_drain  # type: ignore[assignment]

    await worker.send("next turn")
    assert worker._status_task is not first, "send must restart the heartbeat"
    assert worker._last_tool_action is None, "send must clear the last action"
    await asyncio.sleep(0)
    assert first.cancelled() or first.done()
    worker._cancel_status_heartbeat()


@pytest.mark.asyncio
async def test_tool_use_updates_last_action(worker: CcWorker) -> None:
    """The last real tool call is tracked (prefix stripped); StructuredOutput,
    the turn-end control signal, is ignored."""
    worker._handle_event(_tool_use_event("mcp__hamroh__browser_get_text"))
    assert worker._last_tool_action == "browser_get_text", "prefix must be stripped"

    worker._handle_event(_tool_use_event("StructuredOutput"))
    assert worker._last_tool_action == "browser_get_text", "control signal ignored"


@pytest.mark.asyncio
async def test_heartbeat_with_no_callback_is_safe(worker: CcWorker) -> None:
    """No ``on_status`` wired (e.g. tests / headless) must not raise."""
    worker._on_status = None
    worker._arm_status_heartbeat()
    await asyncio.sleep(0.12)
    worker._cancel_status_heartbeat()
    assert worker._current_turn is not None
