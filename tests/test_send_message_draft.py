"""Contract tests for :mod:`pyclaudir.tools.send_message_draft`.

Mocks the Telegram ``Bot`` so we can assert exactly what
``sendMessageDraft`` payloads the tool emits, and what it returns to
Claude on success and failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.send_message_draft import (
    SendMessageDraftArgs,
    SendMessageDraftTool,
)


def _ctx_with_mock_bot(*, on_chat_replied=None) -> ToolContext:
    bot = AsyncMock()
    bot.do_api_request = AsyncMock(return_value=True)
    return ToolContext(bot=bot, on_chat_replied=on_chat_replied)


@pytest.mark.asyncio
async def test_auto_generates_draft_id_when_omitted() -> None:
    ctx = _ctx_with_mock_bot()
    tool = SendMessageDraftTool(ctx)

    result = await tool.run(
        SendMessageDraftArgs(chat_id=42, text="Working on it…")
    )

    assert not result.is_error
    # Auto-generated id must be present in result and in the API call.
    assert result.data is not None
    assigned = result.data["draft_id"]
    assert assigned != 0
    call = ctx.bot.do_api_request.await_args
    assert call.args[0] == "sendMessageDraft"
    assert call.kwargs["api_kwargs"]["draft_id"] == assigned
    assert call.kwargs["api_kwargs"]["chat_id"] == 42
    assert call.kwargs["api_kwargs"]["text"] == "Working on it…"
    # parse_mode omitted → key not in payload (don't want to send a null).
    assert "parse_mode" not in call.kwargs["api_kwargs"]


@pytest.mark.asyncio
async def test_respects_explicit_draft_id_for_in_place_updates() -> None:
    """Claude reuses a draft_id to animate the same bubble — the tool
    must pass it through verbatim."""
    ctx = _ctx_with_mock_bot()
    tool = SendMessageDraftTool(ctx)

    result = await tool.run(
        SendMessageDraftArgs(chat_id=42, draft_id=7777, text="update 2")
    )

    assert result.data["draft_id"] == 7777
    assert ctx.bot.do_api_request.await_args.kwargs["api_kwargs"]["draft_id"] == 7777


@pytest.mark.asyncio
async def test_parse_mode_passes_through_when_set() -> None:
    ctx = _ctx_with_mock_bot()
    tool = SendMessageDraftTool(ctx)

    await tool.run(
        SendMessageDraftArgs(chat_id=1, text="<b>bold</b>", parse_mode="HTML")
    )

    assert ctx.bot.do_api_request.await_args.kwargs["api_kwargs"]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_on_chat_replied_fires_on_success() -> None:
    dismissed: list[int] = []
    ctx = _ctx_with_mock_bot(on_chat_replied=dismissed.append)
    tool = SendMessageDraftTool(ctx)

    await tool.run(SendMessageDraftArgs(chat_id=99, text="hi"))

    assert dismissed == [99]


@pytest.mark.asyncio
async def test_on_chat_replied_does_not_fire_on_failure() -> None:
    """Typing must NOT dismiss if the draft call failed — otherwise the
    user loses both the typing indicator AND the visible draft."""
    dismissed: list[int] = []
    ctx = _ctx_with_mock_bot(on_chat_replied=dismissed.append)
    ctx.bot.do_api_request.side_effect = RuntimeError("telegram 400")
    tool = SendMessageDraftTool(ctx)

    result = await tool.run(SendMessageDraftArgs(chat_id=99, text="hi"))

    assert result.is_error
    assert "sendMessageDraft failed" in result.content
    assert dismissed == []


@pytest.mark.asyncio
async def test_bot_not_configured_is_a_tool_error() -> None:
    ctx = ToolContext(bot=None)
    tool = SendMessageDraftTool(ctx)

    result = await tool.run(SendMessageDraftArgs(chat_id=1, text="x"))

    assert result.is_error
    assert result.content == "bot not configured"


@pytest.mark.asyncio
async def test_does_not_touch_database() -> None:
    """Drafts are ephemeral — unlike send_message, nothing persists."""
    db = AsyncMock()
    ctx = _ctx_with_mock_bot()
    ctx.database = db
    tool = SendMessageDraftTool(ctx)

    await tool.run(SendMessageDraftArgs(chat_id=1, text="x"))

    db.assert_not_called()


def test_text_length_validated_by_pydantic() -> None:
    """Over-limit text rejected by the schema, not passed to Telegram."""
    with pytest.raises(ValidationError):
        SendMessageDraftArgs(chat_id=1, text="x" * 4097)

    with pytest.raises(ValidationError):
        SendMessageDraftArgs(chat_id=1, text="")
