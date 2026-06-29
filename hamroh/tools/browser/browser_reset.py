"""``browser_reset`` — close the current browser tab and clear its state.

Drops the session's context+page so the next ``browser_navigate`` starts from a
clean, isolated context (fresh cookies/localStorage). Use between unrelated
tasks, or to recover a wedged page. Safe to call when nothing is open.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..base import BaseTool, ToolResult


class BrowserResetArgs(BaseModel):
    pass


class BrowserResetTool(BaseTool[BrowserResetArgs]):
    name = "browser_reset"
    description = (
        "Close the current browser page and clear its cookies/state. The next "
        "browser_navigate opens a fresh isolated tab. Use to start clean between "
        "unrelated tasks or to recover a stuck page."
    )
    args_model = BrowserResetArgs

    async def run(self, args: BrowserResetArgs) -> ToolResult:
        if self.ctx.browser_session is None:
            return ToolResult(content="browser session unavailable", is_error=True)
        await self.ctx.browser_session.reset()
        return ToolResult(content="browser reset — navigate to start again")
