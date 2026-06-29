"""``browser_fill`` — type a value into an input on the current browser page.

Fills text fields and textareas for form-driven flows (search boxes, logins).
Acts on the shared session page, so navigate first.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

#: Per-action budget — how long Playwright waits for the field before failing.
_ACTION_TIMEOUT_MS = 10_000


class BrowserFillArgs(BaseModel):
    selector: str = Field(
        min_length=1,
        description="CSS selector of the input or textarea to fill.",
    )
    value: str = Field(
        description="Text to type into the field (replaces any existing value)."
    )


class BrowserFillTool(BrowserSessionTool[BrowserFillArgs]):
    name = "browser_fill"
    description = (
        "Type a value into an input/textarea on the current browser page (call "
        "browser_navigate first). Replaces existing content. Pair with "
        "browser_click to submit."
    )
    args_model = BrowserFillArgs

    async def run(self, args: BrowserFillArgs) -> ToolResult:
        page = self._require_page()
        ok, _ = await try_selector(
            page.fill(args.selector, args.value, timeout=_ACTION_TIMEOUT_MS)
        )
        if not ok:
            return self._miss("fill", args.selector)
        return ToolResult(content=f"filled {args.selector!r}")
