"""``browser_back`` — go back one entry in the current page's history.

Undo a navigation/click that led somewhere unhelpful (e.g. return from an
opened result to the listing). Acts on the shared session page.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..base import ToolResult
from .browser import BrowserSessionTool


class BrowserBackArgs(BaseModel):
    pass


class BrowserBackTool(BrowserSessionTool[BrowserBackArgs]):
    name = "browser_back"
    description = (
        "Go back one entry in the browser history on the current page (call "
        "browser_navigate first). Returns the URL landed on."
    )
    args_model = BrowserBackArgs

    async def run(self, args: BrowserBackArgs) -> ToolResult:
        page = self._require_page()
        await page.go_back()
        return ToolResult(content=f"went back to {page.url}", data={"url": page.url})
