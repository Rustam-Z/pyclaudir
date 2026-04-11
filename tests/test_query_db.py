"""Step 11: query_db tool."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pyclaudir.config import Config
from pyclaudir.db.database import Database
from pyclaudir.db.messages import insert_message
from pyclaudir.models import ChatMessage
from pyclaudir.tools.base import ToolContext
from pyclaudir.tools.query_db import QueryDbArgs, QueryDbTool, is_safe_select


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM messages",
        "SELECT chat_id, COUNT(*) FROM messages GROUP BY chat_id",
        "WITH recent AS (SELECT * FROM messages ORDER BY timestamp DESC LIMIT 5) SELECT * FROM recent",
    ],
)
def test_safe_select_accepts(sql: str) -> None:
    assert is_safe_select(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE messages;",
        "INSERT INTO messages(chat_id) VALUES (1)",
        "DELETE FROM messages",
        "UPDATE messages SET text='x'",
        "PRAGMA journal_mode",
        "ATTACH DATABASE '/tmp/x' AS x",
        "DROP TABLE messages",
        "CREATE TABLE foo(x INT)",
        "",
        "   ",
        "this is not sql",
    ],
)
def test_safe_select_rejects(sql: str) -> None:
    assert is_safe_select(sql) is False, f"accepted: {sql!r}"


def test_safe_select_rejects_cte_with_dml() -> None:
    """A CTE wrapping a DELETE/INSERT/UPDATE must still be rejected."""
    hostile = "WITH bad AS (DELETE FROM messages RETURNING *) SELECT * FROM bad"
    assert is_safe_select(hostile) is False


@pytest.mark.asyncio
async def test_query_db_executes_select(tmp_path: Path) -> None:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        await insert_message(
            db,
            ChatMessage(
                chat_id=-1, message_id=1, user_id=42, direction="in",
                timestamp=datetime(2026, 4, 11, 10, 31, tzinfo=timezone.utc),
                text="hi",
            ),
        )
        ctx = ToolContext(database=db)
        tool = QueryDbTool(ctx)
        result = await tool.run(QueryDbArgs(sql="SELECT direction, text FROM messages"))
        assert result.is_error is False
        assert "direction\ttext" in result.content
        assert "in\thi" in result.content
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_query_db_rejects_hostile_sql(tmp_path: Path) -> None:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        ctx = ToolContext(database=db)
        tool = QueryDbTool(ctx)
        result = await tool.run(QueryDbArgs(sql="DROP TABLE messages"))
        assert result.is_error is True
        assert "rejected" in result.content
    finally:
        await db.close()
