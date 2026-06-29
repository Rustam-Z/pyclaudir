"""``browser_get_html`` — read the HTML of the current page or an element.

When flattened text (``browser_get_text``) loses structure the agent needs —
attributes, nested tags, the exact markup of a widget — this returns the raw
HTML. Truncated to protect the model's context.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool, try_selector

#: HTML is verbose, so a larger cap than get_text — still bounded.
_HTML_LIMIT = 20_000
_LOCATE_TIMEOUT_MS = 5_000


class BrowserGetHtmlArgs(BaseModel):
    selector: str | None = Field(
        default=None,
        description="CSS selector for one element's inner HTML; omit for the full page.",
    )


class BrowserGetHtmlTool(BrowserSessionTool[BrowserGetHtmlArgs]):
    name = "browser_get_html"
    description = (
        "Return the HTML of the current page, or one element's inner HTML with a "
        "selector (call browser_navigate first). Use when structure/attributes "
        f"matter; for plain reading prefer browser_get_text. Truncated to "
        f"{_HTML_LIMIT} characters."
    )
    args_model = BrowserGetHtmlArgs

    async def run(self, args: BrowserGetHtmlArgs) -> ToolResult:
        page = self._require_page()
        if args.selector is not None:
            ok, html = await try_selector(
                page.inner_html(args.selector, timeout=_LOCATE_TIMEOUT_MS)
            )
            if not ok:
                return self._miss("get_html", args.selector)
        else:
            html = await page.content()
        body = html[:_HTML_LIMIT]
        note = (
            f"\n\n[truncated at {_HTML_LIMIT} chars]" if len(html) > _HTML_LIMIT else ""
        )
        return ToolResult(content=f"{body}{note}", data={"chars": len(html)})
