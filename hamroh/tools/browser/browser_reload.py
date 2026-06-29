"""``browser_reload`` — reload the current page.

Re-fetch a page whose content changed or failed to settle. Acts on the shared
session page.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..base import ToolResult
from .browser import BrowserSessionTool


class BrowserReloadArgs(BaseModel):
    pass


class BrowserReloadTool(BrowserSessionTool[BrowserReloadArgs]):
    name = "browser_reload"
    description = (
        "Reload the current browser page (call browser_navigate first). Use when "
        "content changed or didn't finish loading."
    )
    args_model = BrowserReloadArgs

    async def run(self, args: BrowserReloadArgs) -> ToolResult:
        page = self._require_page()
        await page.reload()
        return ToolResult(content=f"reloaded {page.url}", data={"url": page.url})
