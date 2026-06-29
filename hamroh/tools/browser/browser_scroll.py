"""``browser_scroll`` — scroll the current page (or bring an element into view).

Reveals lazy-loaded / infinite-scroll content (image grids, feeds) before
reading or clicking it. Scroll the window by a pixel amount, or scroll a
specific element into view.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

_ACTION_TIMEOUT_MS = 10_000
_DEFAULT_PIXELS = 1_000


class BrowserScrollArgs(BaseModel):
    selector: str | None = Field(
        default=None,
        description="CSS selector to scroll into view; omit to scroll the window.",
    )
    pixels: int = Field(
        default=_DEFAULT_PIXELS,
        description="Vertical pixels to scroll when no selector is given (negative = up).",
    )


class BrowserScrollTool(BrowserSessionTool[BrowserScrollArgs]):
    name = "browser_scroll"
    description = (
        "Scroll the current page (call browser_navigate first) to reveal "
        "lazy-loaded content. Pass a selector to scroll that element into view, "
        "or omit it to scroll the window by `pixels`."
    )
    args_model = BrowserScrollArgs

    async def run(self, args: BrowserScrollArgs) -> ToolResult:
        page = self._require_page()
        if args.selector is not None:
            ok, _ = await try_selector(
                page.locator(args.selector).scroll_into_view_if_needed(
                    timeout=_ACTION_TIMEOUT_MS
                )
            )
            if not ok:
                return self._miss("scroll", args.selector)
            return ToolResult(content=f"scrolled {args.selector!r} into view")
        await page.mouse.wheel(0, args.pixels)
        return ToolResult(content=f"scrolled {args.pixels}px")
