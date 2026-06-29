"""``browser_get_attribute`` — read one attribute of an element.

Gets values ``browser_get_text`` can't see: an ``<img>``'s ``src``, a link's
``href``, an input's ``value``, any ``data-*``. Pair the returned URL with
``browser_download`` to fetch the actual asset.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

#: Short locate budget — a missing element should fail fast, not stall 30s.
_LOCATE_TIMEOUT_MS = 5_000


class BrowserGetAttributeArgs(BaseModel):
    selector: str = Field(
        min_length=1,
        description="CSS selector of the element to read.",
    )
    name: str = Field(
        min_length=1,
        description="Attribute name to read, e.g. 'href', 'src', 'value'.",
    )


class BrowserGetAttributeTool(BrowserSessionTool[BrowserGetAttributeArgs]):
    name = "browser_get_attribute"
    description = (
        "Read one HTML attribute of an element on the current page (call "
        "browser_navigate first), e.g. an image's 'src' or a link's 'href'. "
        "Returns the value, or notes the attribute is absent."
    )
    args_model = BrowserGetAttributeArgs

    async def run(self, args: BrowserGetAttributeArgs) -> ToolResult:
        page = self._require_page()
        ok, value = await try_selector(
            page.get_attribute(args.selector, args.name, timeout=_LOCATE_TIMEOUT_MS)
        )
        if not ok:
            return self._miss("get_attribute", args.selector)
        if value is None:
            return ToolResult(
                content=f"{args.selector!r} has no attribute {args.name!r}",
                data={"value": None},
            )
        return ToolResult(content=value, data={"value": value})
