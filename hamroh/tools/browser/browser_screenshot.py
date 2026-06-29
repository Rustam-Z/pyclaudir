"""``browser_screenshot`` — capture the current browser page as a PNG.

Saves under ``data/renders/`` and returns the image so Claude *sees* it, plus
the path to hand to ``telegram_send_photo`` for delivery. Pass a CSS selector
to capture a single element (e.g. the first image in a results grid) instead of
the whole page.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..base import ToolResult
from .browser import BrowserSessionTool

log = logging.getLogger(__name__)


class BrowserScreenshotArgs(BaseModel):
    selector: str | None = Field(
        default=None,
        description="CSS selector to capture a single element; omit to capture the page.",
    )
    full_page: bool = Field(
        default=True,
        description="Capture the whole scrollable page (ignored when selector is set).",
    )


class BrowserScreenshotTool(BrowserSessionTool[BrowserScreenshotArgs]):
    name = "browser_screenshot"
    description = (
        "Screenshot the current browser page to a PNG under data/renders/ and "
        "return its path (call browser_navigate first). Pass a CSS selector to "
        "capture just one element. Pass the returned path to telegram_send_photo "
        "to deliver it to a chat."
    )
    args_model = BrowserScreenshotArgs

    async def run(self, args: BrowserScreenshotArgs) -> ToolResult:
        store = self.ctx.render_store
        if store is None:
            return ToolResult(content="render store unavailable", is_error=True)
        page = self._require_page()
        out_path = store.allocate("browser")
        if args.selector is not None:
            await page.locator(args.selector).screenshot(path=str(out_path))
        else:
            await page.screenshot(path=str(out_path), full_page=args.full_page)
        relative = store.relative(out_path)
        size = out_path.stat().st_size
        log.info("browser screenshot → %s (%d bytes)", relative, size)
        # Return the PATH (not image_path) — the MCP wrapper drops `content`
        # when image_path is set, which would hide the path the agent needs to
        # call telegram_send_photo.
        return ToolResult(
            content=(
                f"screenshot saved to {relative} ({size} bytes). "
                f"Pass this path to telegram_send_photo to deliver it."
            ),
            data={"path": relative, "size_bytes": size},
        )
