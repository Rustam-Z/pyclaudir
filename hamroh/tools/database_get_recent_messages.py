"""``database_get_recent_messages`` — recent conversation, no SQL required.

A zero-SQL convenience over the ``messages`` table for when the model just
wants "what was said recently" without hand-writing a SELECT through
``database_query``. Returns the most recent real messages (both directions),
oldest-first, as TSV with a header line.

Unlike the restored-context digest, this includes the in-flight turn's own
just-arrived inbound messages (``processed=0``), so the model sees the live
conversation. Deleted and synthetic (``message_id <= 0``) rows are skipped.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..db.messages import RecentMessagesQuery, fetch_recent_messages
from .base import BaseTool, ToolResult

#: Hard cap on rows returned, matching ``database_query``'s ``ROW_CAP``.
MAX_LIMIT = 100
#: Default rows when the caller omits ``limit``.
DEFAULT_LIMIT = 20
#: Per-message text truncation, matching ``database_query``'s ``TEXT_TRUNCATE``.
TEXT_TRUNCATE = 2000

_COLUMNS = ("timestamp", "direction", "message_id", "chat_id", "name", "text")


class DatabaseGetRecentMessagesArgs(BaseModel):
    limit: int = Field(
        default=DEFAULT_LIMIT,
        description=(
            f"How many recent messages to return, oldest-first. "
            f"Defaults to {DEFAULT_LIMIT}, clamped to {MAX_LIMIT}."
        ),
    )
    chat_id: int | None = Field(
        default=None,
        description="Restrict to one chat. Omit to span all chats.",
    )
    before_message_id: int | None = Field(
        default=None,
        description=(
            "Return only messages older than this message id, for paging back "
            "through history. Message ids are per-chat, so pass it with chat_id."
        ),
    )


def _render_row(row: dict) -> str:
    """One message as a TSV line over ``_COLUMNS``, text truncated."""
    name = row["first_name"] or row["username"] or str(row["user_id"])
    text = row["text"] or ""
    if len(text) > TEXT_TRUNCATE:
        text = text[:TEXT_TRUNCATE] + "…[truncated]"
    cells = (
        str(row["timestamp"]),
        str(row["direction"]),
        str(row["message_id"]),
        str(row["chat_id"]),
        str(name),
        text,
    )
    return "\t".join(cells)


class DatabaseGetRecentMessagesTool(BaseTool[DatabaseGetRecentMessagesArgs]):
    name = "database_get_recent_messages"
    description = (
        "Return the most recent messages (both directions), oldest-first, as "
        "TSV with a header line. Includes the current turn's own inbound "
        "messages. Use this for quick conversation recall; use database_query "
        "for filtering, joins, or other tables. Capped at 100 rows; text "
        "columns truncated to 2000 chars."
    )
    args_model = DatabaseGetRecentMessagesArgs

    async def run(self, args: DatabaseGetRecentMessagesArgs) -> ToolResult:
        if self.ctx.database is None:
            return ToolResult(content="database unavailable", is_error=True)
        limit = max(1, min(args.limit, MAX_LIMIT))
        rows = await fetch_recent_messages(
            self.ctx.database,
            RecentMessagesQuery(
                limit=limit,
                include_unprocessed=True,
                chat_id=args.chat_id,
                before_message_id=args.before_message_id,
            ),
        )
        if not rows:
            return ToolResult(content="(no rows)")
        lines = ["\t".join(_COLUMNS)]
        lines.extend(_render_row(r) for r in rows)
        return ToolResult(content="\n".join(lines), data={"row_count": len(rows)})
