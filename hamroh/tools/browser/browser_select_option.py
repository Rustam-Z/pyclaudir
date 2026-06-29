"""``browser_select_option`` — choose an option in a ``<select>`` dropdown.

``browser_fill`` can't drive native dropdowns; this picks an option by its
value (or visible label). Acts on the shared session page.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

_ACTION_TIMEOUT_MS = 10_000


class BrowserSelectOptionArgs(BaseModel):
    selector: str = Field(
        min_length=1,
        description="CSS selector of the <select> element.",
    )
    value: str = Field(
        min_length=1,
        description="Option to choose — matched against its value, then its label.",
    )


class BrowserSelectOptionTool(BrowserSessionTool[BrowserSelectOptionArgs]):
    name = "browser_select_option"
    description = (
        "Choose an option in a <select> dropdown on the current page (call "
        "browser_navigate first). Use this instead of browser_fill for native "
        "dropdowns."
    )
    args_model = BrowserSelectOptionArgs

    async def run(self, args: BrowserSelectOptionArgs) -> ToolResult:
        page = self._require_page()
        ok, chosen = await try_selector(
            page.select_option(args.selector, args.value, timeout=_ACTION_TIMEOUT_MS)
        )
        if not ok:
            return self._miss("select_option", args.selector)
        return ToolResult(
            content=f"selected {args.value!r} in {args.selector!r}",
            data={"selected": chosen},
        )
