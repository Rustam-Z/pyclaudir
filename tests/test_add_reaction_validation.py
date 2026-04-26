"""Verify ``add_reaction`` rejects emojis Telegram won't accept."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyclaudir.tools.add_reaction import AddReactionArgs, AddReactionTool
from pyclaudir.tools.base import ToolContext


class _BotStub:
    def __init__(self) -> None:
        self.set_calls: list[dict[str, Any]] = []

    async def set_message_reaction(self, **kwargs: Any) -> None:
        self.set_calls.append(kwargs)

    async def get_me(self) -> Any:
        return SimpleNamespace(id=42)


def _tool(bot: _BotStub) -> AddReactionTool:
    return AddReactionTool(ToolContext(bot=bot, database=None))


@pytest.mark.asyncio
async def test_rejects_unsupported_emoji() -> None:
    bot = _BotStub()
    tool = _tool(bot)
    result = await tool.run(AddReactionArgs(chat_id=1, message_id=2, emoji="🙅"))
    assert result.is_error
    assert "🙅" in result.content
    assert bot.set_calls == []


@pytest.mark.asyncio
async def test_strips_vs16_before_calling_telegram() -> None:
    bot = _BotStub()
    tool = _tool(bot)
    result = await tool.run(AddReactionArgs(chat_id=1, message_id=2, emoji="❤️"))
    assert not result.is_error
    assert len(bot.set_calls) == 1
    assert bot.set_calls[0]["reaction"][0].emoji == "❤"
