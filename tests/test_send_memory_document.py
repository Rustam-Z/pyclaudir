"""``send_memory_document``: path safety, missing file, happy path, callbacks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyclaudir.storage.memory import MemoryStore
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.send_memory_document import (
    SendMemoryDocumentArgs,
    SendMemoryDocumentTool,
)


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path / "memories")
    s.ensure_root()
    return s


def _mock_bot(message_id: int = 999) -> MagicMock:
    bot = MagicMock()
    bot.send_document = AsyncMock(return_value=MagicMock(message_id=message_id))
    bot.get_me = AsyncMock(
        return_value=MagicMock(id=1, username="bot", first_name="Bot")
    )
    return bot


@pytest.mark.asyncio
async def test_happy_path_sends_document(store: MemoryStore) -> None:
    store.write("notes/report.md", "# Report\nbody")
    bot = _mock_bot(message_id=42)
    tool = SendMemoryDocumentTool(ToolContext(bot=bot, memory_store=store))

    result = await tool.run(
        SendMemoryDocumentArgs(chat_id=123, path="notes/report.md")
    )

    assert result.is_error is False
    assert "message_id=42" in result.content
    assert result.data == {
        "message_id": 42,
        "chat_id": 123,
        "filename": "report.md",
        "path": "notes/report.md",
    }
    bot.send_document.assert_awaited_once()
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs["filename"] == "report.md"
    assert Path(kwargs["document"]).read_text() == "# Report\nbody"
    assert kwargs["caption"] is None
    assert kwargs["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_caption_and_reply_to_passed_through(store: MemoryStore) -> None:
    store.write("a.md", "x")
    bot = _mock_bot()
    tool = SendMemoryDocumentTool(ToolContext(bot=bot, memory_store=store))

    await tool.run(
        SendMemoryDocumentArgs(
            chat_id=7, path="a.md", caption="here you go", reply_to_message_id=55
        )
    )

    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["caption"] == "here you go"
    assert kwargs["reply_to_message_id"] == 55


@pytest.mark.asyncio
async def test_path_traversal_rejected(store: MemoryStore) -> None:
    bot = _mock_bot()
    tool = SendMemoryDocumentTool(ToolContext(bot=bot, memory_store=store))

    result = await tool.run(
        SendMemoryDocumentArgs(chat_id=1, path="../etc/passwd")
    )

    assert result.is_error is True
    assert "MemoryPathError" in result.content
    bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_absolute_path_rejected(store: MemoryStore) -> None:
    bot = _mock_bot()
    tool = SendMemoryDocumentTool(ToolContext(bot=bot, memory_store=store))

    result = await tool.run(
        SendMemoryDocumentArgs(chat_id=1, path="/etc/passwd")
    )

    assert result.is_error is True
    bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_file_returns_error_without_upload(store: MemoryStore) -> None:
    bot = _mock_bot()
    tool = SendMemoryDocumentTool(ToolContext(bot=bot, memory_store=store))

    result = await tool.run(
        SendMemoryDocumentArgs(chat_id=1, path="does/not/exist.md")
    )

    assert result.is_error is True
    assert "not found" in result.content
    bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_bot_returns_error(store: MemoryStore) -> None:
    tool = SendMemoryDocumentTool(ToolContext(bot=None, memory_store=store))
    result = await tool.run(SendMemoryDocumentArgs(chat_id=1, path="a.md"))
    assert result.is_error is True
    assert "bot not configured" in result.content


@pytest.mark.asyncio
async def test_no_memory_store_returns_error() -> None:
    bot = _mock_bot()
    tool = SendMemoryDocumentTool(ToolContext(bot=bot, memory_store=None))
    result = await tool.run(SendMemoryDocumentArgs(chat_id=1, path="a.md"))
    assert result.is_error is True
    assert "memory store" in result.content


@pytest.mark.asyncio
async def test_on_chat_replied_invoked_after_send(store: MemoryStore) -> None:
    store.write("note.md", "hi")
    bot = _mock_bot()
    seen: list[int] = []
    ctx = ToolContext(
        bot=bot, memory_store=store, on_chat_replied=lambda cid: seen.append(cid)
    )
    tool = SendMemoryDocumentTool(ctx)

    await tool.run(SendMemoryDocumentArgs(chat_id=999, path="note.md"))

    assert seen == [999]
