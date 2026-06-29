"""``browser_press_key`` — press a keyboard key on the current page.

The natural way to submit a search box (``Enter``) or move focus (``Tab``)
without hunting for a button. Target a specific element with ``selector``, or
omit it to send the key to whatever has focus.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

_ACTION_TIMEOUT_MS = 10_000


class BrowserPressKeyArgs(BaseModel):
    key: str = Field(
        min_length=1,
        description="Key to press, e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown'.",
    )
    selector: str | None = Field(
        default=None,
        description="CSS selector to focus first; omit to send to the focused element.",
    )


class BrowserPressKeyTool(BrowserSessionTool[BrowserPressKeyArgs]):
    name = "browser_press_key"
    description = (
        "Press a keyboard key on the current page (call browser_navigate first). "
        "Use 'Enter' to submit a search/form, 'Tab' to move focus. Pass a "
        "selector to target a field, else the key goes to the focused element."
    )
    args_model = BrowserPressKeyArgs

    async def run(self, args: BrowserPressKeyArgs) -> ToolResult:
        page = self._require_page()
        if args.selector is not None:
            ok, _ = await try_selector(
                page.press(args.selector, args.key, timeout=_ACTION_TIMEOUT_MS)
            )
            if not ok:
                return self._miss("press_key", args.selector)
        else:
            await page.keyboard.press(args.key)
        return ToolResult(content=f"pressed {args.key!r}")
