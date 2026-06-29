"""``browser_get_text`` — extract visible text from the current browser page.

Reads ``inner_text`` of the whole page (or a CSS-scoped element) so the bot
can understand a JS-rendered page that ``WebFetch`` can't. Truncated to keep
the model's context from overflowing on huge pages.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

#: Max characters returned before truncating — keeps big pages from flooding
#: the model's context.
_TEXT_LIMIT = 10_000

#: How long to wait for the selector before failing — short, so a missing
#: element errors fast instead of stalling on Playwright's 30s default.
_LOCATE_TIMEOUT_MS = 5_000


class BrowserGetTextArgs(BaseModel):
    selector: str = Field(
        default="body",
        min_length=1,
        description="CSS selector to scope extraction; defaults to the whole page body.",
    )


class BrowserGetTextTool(BrowserSessionTool[BrowserGetTextArgs]):
    name = "browser_get_text"
    description = (
        "Return the visible text of the current browser page (call "
        "browser_navigate first). Pass a CSS selector to scope to one element, "
        f"else the whole body. Truncated to {_TEXT_LIMIT} characters."
    )
    args_model = BrowserGetTextArgs

    async def run(self, args: BrowserGetTextArgs) -> ToolResult:
        page = self._require_page()
        ok, text = await try_selector(
            page.inner_text(args.selector, timeout=_LOCATE_TIMEOUT_MS)
        )
        if not ok:
            return self._miss("get_text", args.selector)
        body = text[:_TEXT_LIMIT]
        note = (
            f"\n\n[truncated at {_TEXT_LIMIT} chars]" if len(text) > _TEXT_LIMIT else ""
        )
        return ToolResult(content=f"{body}{note}", data={"chars": len(text)})
