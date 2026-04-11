"""``now`` — return the current time. Smoke test for the MCP wiring."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from .base import BaseTool, ToolResult


class NowArgs(BaseModel):
    pass


class NowTool(BaseTool):
    name = "now"
    description = (
        "Return the current time as ISO8601 in both UTC and the host's local timezone. "
        "Useful as a basic 'is the tool channel alive' check."
    )
    args_model = NowArgs

    async def run(self, args: NowArgs) -> ToolResult:
        utc = datetime.now(timezone.utc)
        local = datetime.now().astimezone()
        return ToolResult(
            content=(
                f"utc={utc.isoformat(timespec='seconds')} "
                f"local={local.isoformat(timespec='seconds')}"
            ),
            data={
                "utc": utc.isoformat(timespec="seconds"),
                "local": local.isoformat(timespec="seconds"),
            },
        )
