"""``/reset_session`` — owner-only escape hatch for unbounded CC context.

The reset happens in-process: the worker drops the session id and the
supervisor respawns the ``claude`` subprocess fresh. The bot itself must
stay up — no SIGTERM, no reliance on an external supervisor.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyclaudir.access import AccessConfig, save_access
from pyclaudir.cc_worker import CcSpawnSpec, CcWorker, TurnResult
from pyclaudir.config import Config
from pyclaudir.engine import Engine
from pyclaudir.telegram_io import TelegramDispatcher

OWNER = 42
STRANGER = 100


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    object.__setattr__(cfg, "owner_id", OWNER)
    save_access(
        cfg.access_path,
        AccessConfig(policy="owner_only", allowed_users=[], allowed_chats=[]),
    )
    return cfg


def _update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _dispatcher(cfg: Config) -> tuple[TelegramDispatcher, MagicMock]:
    engine = MagicMock(reset_session=AsyncMock())
    return TelegramDispatcher(cfg, MagicMock(), engine=engine, chat_titles={}), engine


# ----------------------------------------------------------------------
# Dispatcher command
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_session_clears_id_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a persisted session id and an owner-issued /reset_session
    cfg = _cfg(tmp_path)
    cfg.session_id_path.write_text("abc-123")
    dispatcher, engine = _dispatcher(cfg)
    kills: list[int] = []
    monkeypatch.setattr(
        "pyclaudir.telegram_io.commands.os.kill",
        lambda _pid, sig: kills.append(sig),
    )
    update = _update(OWNER)

    # When the command runs
    await dispatcher._cmd_reset_session(update, MagicMock())

    # Then the id file is gone, the engine resets in-process, the owner
    # got a reply, and the bot process was NOT killed
    assert not cfg.session_id_path.exists(), "session id file must be deleted"
    engine.reset_session.assert_awaited_once()
    update.effective_message.reply_text.assert_awaited()
    assert kills == [], "bot must stay up — reset is in-process, not a SIGTERM"


@pytest.mark.asyncio
async def test_reset_session_ignores_non_owner(tmp_path: Path) -> None:
    # Given a persisted session id and a stranger-issued /reset_session
    cfg = _cfg(tmp_path)
    cfg.session_id_path.write_text("abc-123")
    dispatcher, engine = _dispatcher(cfg)

    # When the command runs
    await dispatcher._cmd_reset_session(_update(STRANGER), MagicMock())

    # Then nothing happens
    assert cfg.session_id_path.exists(), "stranger must not clear the session"
    engine.reset_session.assert_not_awaited()


# ----------------------------------------------------------------------
# Worker reset
# ----------------------------------------------------------------------


def _spec(tmp_path: Path) -> CcSpawnSpec:
    sp = tmp_path / "system.md"
    sp.write_text("system")
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {}}')
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    return CcSpawnSpec(
        binary="/bin/true",  # we never actually spawn
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
        session_id="abc-123",
    )


def _worker_with_stubbed_terminate(tmp_path: Path) -> tuple[CcWorker, list[bool]]:
    worker = CcWorker(_spec(tmp_path), Config.for_test(tmp_path))
    terminated: list[bool] = []

    async def fake_terminate() -> None:
        terminated.append(True)

    worker._terminate_proc = fake_terminate  # type: ignore[method-assign]
    return worker, terminated


@pytest.mark.asyncio
async def test_worker_reset_mid_turn_drops_id_and_unblocks_engine(
    tmp_path: Path,
) -> None:
    # Given a worker resumed on a session, mid-turn
    worker, terminated = _worker_with_stubbed_terminate(tmp_path)
    worker._current_turn = TurnResult()

    # When the session reset runs
    await worker.reset_session()

    # Then the session id is gone everywhere, the supervisor sees an
    # intentional abort, and a sentinel unblocks the waiting engine
    assert worker.spec.session_id is None, "respawn must omit --resume"
    assert worker.session_id is None, "shutdown must not re-persist the old id"
    assert worker._supervisor_abort_reason == "session-reset", (
        "supervisor must respawn without consuming the crash budget"
    )
    assert terminated, "subprocess must be terminated to trigger the respawn"
    assert worker._current_turn is None, "in-flight turn must be dropped"
    sentinel = worker._result_queue.get_nowait()
    assert sentinel.aborted_reason == "session-reset", (
        "engine must be unblocked with the reset sentinel"
    )


@pytest.mark.asyncio
async def test_worker_reset_when_idle_queues_no_sentinel(tmp_path: Path) -> None:
    # Given an idle worker (no turn in flight)
    worker, terminated = _worker_with_stubbed_terminate(tmp_path)

    # When the session reset runs
    await worker.reset_session()

    # Then no sentinel is queued — one would poison the next turn
    assert worker._result_queue.empty(), (
        "idle reset must not queue a sentinel for the next turn to consume"
    )
    assert worker.spec.session_id is None, "respawn must omit --resume"
    assert terminated, "subprocess must be terminated to trigger the respawn"


# ----------------------------------------------------------------------
# Engine sentinel handling
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_discards_callbacks_on_reset_sentinel(tmp_path: Path) -> None:
    # Given an engine mid-turn with a queued reminder callback
    engine = Engine(MagicMock(), Config.for_test(tmp_path))
    engine._is_processing.set()
    engine._turn.active_chats = {-100}
    fired: list[bool] = []

    async def callback() -> None:
        fired.append(True)

    engine._turn_callbacks = [callback]

    # When the reset sentinel arrives
    await engine._handle_turn_result(TurnResult(aborted_reason="session-reset"))

    # Then the turn ends quietly: callbacks are discarded (reminders stay
    # pending and retry post-reset) and the engine is idle again
    assert not engine._is_processing.is_set(), "engine must be idle after reset"
    assert fired == [], "callbacks must NOT fire — CC never finished the turn"
    assert engine._turn_callbacks == [], "callbacks must be discarded"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"
