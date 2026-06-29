"""``browser_list`` — enumerate elements matching a selector.

Instead of guessing one exact selector, list everything matching a broad one
(every link, image, result row) with its text and ``href``/``src`` so the agent
can pick the right one to click or download.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool

#: Cap the number of elements reported so a huge page can't flood context.
_LIST_LIMIT = 50


async def _describe(element) -> str:
    """One line per element: trimmed text plus its href/src if present."""
    text = (await element.inner_text()).strip().replace("\n", " ")[:120]
    link = await element.get_attribute("href") or await element.get_attribute("src")
    return f"- {text}" + (f"  [{link}]" if link else "")


class BrowserListArgs(BaseModel):
    selector: str = Field(
        min_length=1,
        description="CSS selector matching the elements to list, e.g. 'a', 'img', '.result'.",
    )


class BrowserListTool(BrowserSessionTool[BrowserListArgs]):
    name = "browser_list"
    description = (
        "List the elements matching a selector on the current page (call "
        "browser_navigate first) with their text and href/src — handy for "
        f"choosing which link/image to act on. Reports up to {_LIST_LIMIT}."
    )
    args_model = BrowserListArgs

    async def run(self, args: BrowserListArgs) -> ToolResult:
        page = self._require_page()
        elements = await page.query_selector_all(args.selector)
        lines = [await _describe(el) for el in elements[:_LIST_LIMIT]]
        extra = len(elements) - _LIST_LIMIT
        more = f"\n…(+{extra} more)" if extra > 0 else ""
        body = "\n".join(lines) if lines else "(no matches)"
        return ToolResult(content=f"{body}{more}", data={"count": len(elements)})
