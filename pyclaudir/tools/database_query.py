"""``database_query`` — read-only SQL access to the agent's own SQLite.

Inputs are parsed with sqlglot and **rejected** unless the entire statement
list is exactly one ``SELECT`` (or a SELECT with a CTE that contains nothing
but more SELECTs). No semicolons, no DML, no PRAGMA, no ATTACH, no nothing.
Results are capped at 100 rows; text columns are truncated to 2000 chars.
"""

from __future__ import annotations


import sqlglot
from sqlglot import exp
from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult

ROW_CAP = 100
TEXT_TRUNCATE = 2000


def parse_safe_select(sql: str) -> exp.Select | None:
    """Return the parsed ``Select`` iff ``sql`` is a single read-only SELECT.

    Returns ``None`` for anything else: multiple statements, non-SELECT roots,
    or trees containing mutation/PRAGMA/ATTACH/etc.
    """
    if not isinstance(sql, str) or not sql.strip():
        return None
    try:
        statements = sqlglot.parse(sql, dialect="sqlite")
    except Exception:
        return None
    if len(statements) != 1:
        return None
    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        return None
    forbidden = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Pragma,
        exp.Attach,
        exp.Detach,
        exp.TruncateTable,
        exp.Merge,
        exp.Into,
    )
    for node in stmt.walk():
        target = node[0] if isinstance(node, tuple) else node
        if isinstance(target, forbidden):
            return None
    return stmt


def is_safe_select(sql: str) -> bool:
    """True iff ``sql`` is a single read-only SELECT under sqlglot's parser."""
    return parse_safe_select(sql) is not None


def cap_limit(stmt: exp.Select, row_cap: int) -> exp.Select:
    """Ensure ``stmt`` has a LIMIT clause no larger than ``row_cap``.

    - No existing LIMIT → adds ``LIMIT row_cap``.
    - Existing literal LIMIT ≤ row_cap → leaves it alone.
    - Existing literal LIMIT > row_cap, or non-literal expression → overwrites
      with ``LIMIT row_cap``.
    """
    existing = stmt.args.get("limit")
    if existing is None:
        return stmt.limit(row_cap, copy=False)
    expr = existing.expression
    if isinstance(expr, exp.Literal) and not expr.is_string:
        try:
            if int(expr.this) <= row_cap:
                return stmt
        except (TypeError, ValueError):
            pass
    return stmt.limit(row_cap, copy=False)


class DatabaseQueryArgs(BaseModel):
    sql: str = Field(
        description=(
            "A single SELECT statement. No semicolons, no DML, no PRAGMA. "
            "Capped at 100 rows; text columns truncated to 2000 chars."
        )
    )


class DatabaseQueryTool(BaseTool[DatabaseQueryArgs]):
    name = "database_query"
    description = (
        "Run a single read-only SELECT against the agent's local SQLite. "
        "Returns rows as TSV with a header line. Capped at 100 rows; text "
        "columns truncated to 2000 chars. If the query already has a LIMIT, "
        "it is respected (and clamped down to 100 if larger).\n"
        "\n"
        "Tables and their time columns:\n"
        "  messages(chat_id, message_id, user_id, username, first_name,\n"
        "           direction, timestamp, text, reply_to_id, reply_to_text,\n"
        "           edited, deleted)  -- time column: `timestamp` "
        "(NOT created_at)\n"
        "  users(chat_id, user_id, username, first_name, join_date,\n"
        "        last_message_date, message_count)\n"
        "  reactions(id, chat_id, message_id, user_id, emoji, created_at)\n"
        "  tool_calls(id, tool_name, args_json, result_json, error,\n"
        "             duration_ms, created_at)\n"
        "  reminders -- see reminder_set docs"
    )
    args_model = DatabaseQueryArgs

    async def run(self, args: DatabaseQueryArgs) -> ToolResult:
        if self.ctx.database is None:
            return ToolResult(content="database unavailable", is_error=True)
        stmt = parse_safe_select(args.sql)
        if stmt is None:
            return ToolResult(
                content="rejected: only single read-only SELECT statements are allowed",
                is_error=True,
            )

        capped = cap_limit(stmt, ROW_CAP).sql(dialect="sqlite")
        try:
            rows = await self.ctx.database.fetch_all(capped)
        except Exception as exc:
            return ToolResult(content=f"sql error: {exc}", is_error=True)

        if not rows:
            return ToolResult(content="(no rows)")

        cols = list(rows[0].keys())
        out_lines = ["\t".join(cols)]
        for row in rows:
            cells: list[str] = []
            for col in cols:
                value = row[col]
                if isinstance(value, str) and len(value) > TEXT_TRUNCATE:
                    value = value[:TEXT_TRUNCATE] + "…[truncated]"
                cells.append("" if value is None else str(value))
            out_lines.append("\t".join(cells))
        return ToolResult(content="\n".join(out_lines), data={"row_count": len(rows)})
