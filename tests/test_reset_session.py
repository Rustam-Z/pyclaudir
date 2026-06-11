"""``/reset_session`` — owner-only escape hatch for unbounded CC context."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyclaudir.access import AccessConfig, save_access
from pyclaudir.config import Config
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


def _dispatcher(cfg: Config) -> TelegramDispatcher:
    return TelegramDispatcher(cfg, MagicMock(), engine=None, chat_titles={})


@pytest.mark.asyncio
async def test_reset_session_clears_id_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a persisted session id and an owner-issued /reset_session
    cfg = _cfg(tmp_path)
    cfg.session_id_path.write_text("abc-123")
    dispatcher = _dispatcher(cfg)
    kills: list[int] = []
    monkeypatch.setattr(
        "pyclaudir.telegram_io.dispatcher.os.kill",
        lambda _pid, sig: kills.append(sig),
    )
    update = _update(OWNER)

    # When the command runs
    await dispatcher._cmd_reset_session(update, MagicMock())

    # Then the id file is gone, the teardown skip-flag is set, the owner
    # got a reply, and the process restarts via SIGTERM
    assert not cfg.session_id_path.exists(), "session id file must be deleted"
    assert dispatcher.session_reset_requested is True, "shutdown must not re-persist the id"
    update.effective_message.reply_text.assert_awaited()
    assert kills, "process must SIGTERM itself so the supervisor restarts it fresh"


@pytest.mark.asyncio
async def test_reset_session_ignores_non_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    cfg.session_id_path.write_text("abc-123")
    dispatcher = _dispatcher(cfg)
    kills: list[int] = []
    monkeypatch.setattr(
        "pyclaudir.telegram_io.dispatcher.os.kill",
        lambda _pid, sig: kills.append(sig),
    )

    await dispatcher._cmd_reset_session(_update(STRANGER), MagicMock())

    assert cfg.session_id_path.exists(), "stranger must not clear the session"
    assert dispatcher.session_reset_requested is False
    assert kills == [], "stranger must not restart the bot"
