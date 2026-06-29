"""``browser_wait_for`` — wait for an element to appear on the current page.

Bridges async UI: after a click or navigation, wait for the next element to
render before reading or acting on it. Acts on the shared session page.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

_WAIT_DEFAULT_MS = 10_000
_WAIT_MAX_MS = 30_000


class BrowserWaitForArgs(BaseModel):
    selector: str = Field(
        min_length=1,
        description="CSS selector to wait for until it appears in the DOM.",
    )
    timeout_ms: int = Field(
        default=_WAIT_DEFAULT_MS,
        ge=1,
        le=_WAIT_MAX_MS,
        description=f"How long to wait in milliseconds (default {_WAIT_DEFAULT_MS}, max {_WAIT_MAX_MS}).",
    )


class BrowserWaitForTool(BrowserSessionTool[BrowserWaitForArgs]):
    name = "browser_wait_for"
    description = (
        "Wait for an element to appear on the current browser page (call "
        "browser_navigate first). Use after a click that loads content "
        "asynchronously, before reading or acting on it."
    )
    args_model = BrowserWaitForArgs

    async def run(self, args: BrowserWaitForArgs) -> ToolResult:
        page = self._require_page()
        ok, _ = await try_selector(
            page.wait_for_selector(args.selector, timeout=args.timeout_ms)
        )
        if not ok:
            return self._miss("wait_for", args.selector)
        return ToolResult(content=f"element {args.selector!r} is present")
