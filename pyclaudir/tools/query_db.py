"""``query_db`` — read-only SQL access to Nodira's own SQLite.

Inputs are parsed with sqlglot and **rejected** unless the entire statement
list is exactly one ``SELECT`` (or a SELECT with a CTE that contains nothing
but more SELECTs). No semicolons, no DML, no PRAGMA, no ATTACH, no nothing.
Results are capped at 100 rows; text columns are truncated to 2000 chars.
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp
from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult

ROW_CAP = 100
TEXT_TRUNCATE = 2000


def is_safe_select(sql: str) -> bool:
    """True iff ``sql`` is a single read-only SELECT under sqlglot's parser."""
    if not isinstance(sql, str) or not sql.strip():
        return False
    try:
        statements = sqlglot.parse(sql, dialect="sqlite")
    except Exception:
        return False
    if len(statements) != 1:
        return False
    stmt = statements[0]
    if stmt is None:
        return False
    # The top-level statement must be a Select.
    if not isinstance(stmt, exp.Select):
        return False
    # Walk the entire tree and reject any node that represents mutation,
    # PRAGMA, ATTACH, or anything else with side effects.
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
            return False
    return True


class QueryDbArgs(BaseModel):
    sql: str = Field(
        description=(
            "A single SELECT statement. No semicolons, no DML, no PRAGMA. "
            "Capped at 100 rows; text columns truncated to 2000 chars."
        )
    )


class QueryDbTool(BaseTool):
    name = "query_db"
    description = (
        "Run a single read-only SELECT against Nodira's local SQLite. "
        "Useful for looking up message history, user activity, prior tool "
        "calls. Returns rows as TSV with a header line."
    )
    args_model = QueryDbArgs

    async def run(self, args: QueryDbArgs) -> ToolResult:
        if self.ctx.database is None:
            return ToolResult(content="database unavailable", is_error=True)
        if not is_safe_select(args.sql):
            return ToolResult(
                content="rejected: only single read-only SELECT statements are allowed",
                is_error=True,
            )

        capped = args.sql.rstrip().rstrip(";")
        capped = f"{capped} LIMIT {ROW_CAP}"
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
