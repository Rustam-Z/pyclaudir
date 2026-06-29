"""``browser_click`` — click an element on the current browser page.

Drives multi-step flows: follow a link, open a menu, submit a form. Acts on
the shared session page, so navigate first.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

#: Per-action budget — how long Playwright waits for the element to be
#: clickable before giving up.
_ACTION_TIMEOUT_MS = 10_000


class BrowserClickArgs(BaseModel):
    selector: str = Field(
        min_length=1,
        description="CSS selector of the element to click.",
    )


class BrowserClickTool(BrowserSessionTool[BrowserClickArgs]):
    name = "browser_click"
    description = (
        "Click an element on the current browser page (call browser_navigate "
        "first). Waits up to ~10s for the element to be actionable."
    )
    args_model = BrowserClickArgs

    async def run(self, args: BrowserClickArgs) -> ToolResult:
        page = self._require_page()
        ok, _ = await try_selector(
            page.click(args.selector, timeout=_ACTION_TIMEOUT_MS)
        )
        if not ok:
            return self._miss("click", args.selector)
        return ToolResult(content=f"clicked {args.selector!r}")
