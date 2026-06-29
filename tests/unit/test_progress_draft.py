"""Live "working…" progress draft for long turns.

When ``progress_draft_enabled`` is on, the engine's per-chat liveness signal
shows a streaming Telegram draft (``sendMessageDraft``) in DMs instead of the
plain "typing…" action, so a slow job visibly reports progress. Telegram only
allows drafts in private chats, so groups must keep the typing indicator. These
tests pin both the text builder and the DM-vs-group routing in
``_make_typing_action``.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from hamroh.config import Config
from hamroh.startup import (
    PROGRESS_DRAFT_ID,
    _App,
    _make_typing_action,
    _progress_draft_text,
)

DM_CHAT = 42  # private chats have positive ids
GROUP_CHAT = -1001234567890  # supergroups are negative


def _app(tmp_path: Path, *, draft_enabled: bool) -> tuple[_App, MagicMock]:
    """An _App wired with a mock bot + worker; returns the dispatcher mock too."""
    cfg = replace(Config.for_test(tmp_path), progress_draft_enabled=draft_enabled)
    app = _App(config=cfg, db=MagicMock())
    dispatcher = MagicMock()
    dispatcher.bot.send_chat_action = AsyncMock(return_value=True)
    dispatcher.bot.send_message_draft = AsyncMock(return_value=True)
    app.dispatcher = dispatcher
    app.worker = MagicMock()
    app.worker._turn_started_at = time.monotonic() - 5.0
    app.worker._last_tool_action = "browser_navigate"
    return app, dispatcher


# ----- _progress_draft_text (pure) -----


def test_progress_text_seconds_with_step() -> None:
    text = _progress_draft_text(elapsed=5.0, last_action="browser_navigate")
    assert "~5s" in text, f"sub-minute elapsed should show seconds: {text!r}"
    assert "browser_navigate" in text, f"last step should be named: {text!r}"


def test_progress_text_minutes_without_step() -> None:
    text = _progress_draft_text(elapsed=125.0, last_action=None)
    assert "~2 min" in text, f"elapsed over a minute should show minutes: {text!r}"
    assert "last step" not in text, f"no step should be shown when None: {text!r}"


def test_progress_text_is_never_empty() -> None:
    # Telegram treats empty draft text as a bare "Thinking…" placeholder, so we
    # always send a non-empty line even at the very start of a turn.
    assert _progress_draft_text(elapsed=0.0, last_action=None).strip(), (
        "draft text must not be empty"
    )


# ----- _make_typing_action routing -----


async def test_dm_uses_draft_when_enabled(tmp_path: Path) -> None:
    # given a DM and the progress-draft feature on
    app, dispatcher = _app(tmp_path, draft_enabled=True)
    action = _make_typing_action(dispatcher, app)

    # when the engine fires the liveness signal for that chat
    await action(DM_CHAT)

    # then a draft is streamed and no typing action is sent
    dispatcher.bot.send_message_draft.assert_awaited_once()
    kwargs = dispatcher.bot.send_message_draft.await_args.kwargs
    assert kwargs["chat_id"] == DM_CHAT, "draft must target the same chat"
    assert kwargs["draft_id"] == PROGRESS_DRAFT_ID != 0, "draft_id must be non-zero"
    assert kwargs["text"], "draft must carry progress text"
    dispatcher.bot.send_chat_action.assert_not_awaited()


async def test_group_falls_back_to_typing_even_when_enabled(tmp_path: Path) -> None:
    # given a group chat and the feature on (drafts are private-chat only)
    app, dispatcher = _app(tmp_path, draft_enabled=True)
    action = _make_typing_action(dispatcher, app)

    # when the liveness signal fires for the group
    await action(GROUP_CHAT)

    # then it sends the typing action, never a draft
    dispatcher.bot.send_chat_action.assert_awaited_once_with(
        chat_id=GROUP_CHAT, action="typing"
    )
    dispatcher.bot.send_message_draft.assert_not_awaited()


async def test_dm_uses_typing_when_disabled(tmp_path: Path) -> None:
    # given a DM but the feature off (default)
    app, dispatcher = _app(tmp_path, draft_enabled=False)
    action = _make_typing_action(dispatcher, app)

    # when the liveness signal fires
    await action(DM_CHAT)

    # then it sends the plain typing action, never a draft
    dispatcher.bot.send_chat_action.assert_awaited_once_with(
        chat_id=DM_CHAT, action="typing"
    )
    dispatcher.bot.send_message_draft.assert_not_awaited()
