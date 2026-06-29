"""``browser_navigate`` — open a URL in the shared headless browser session.

First tool in any browser flow: it lazily opens the persistent page (issue
#50's ``browser_open``) and loads ``url``. Later ``browser_*`` tools act on the
same page. Outbound network is ALLOWED here (unlike ``render_html``) so the bot
can read real pages — but a private-IP/``file://`` guard blocks internal targets.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from .ssrf import check_navigable

log = logging.getLogger(__name__)

#: Page-load budget. Generous — JS-heavy pages can be slow to settle.
_NAV_TIMEOUT_MS = 30_000


class BrowserNavigateArgs(BaseModel):
    url: str = Field(
        min_length=1,
        description=(
            "Absolute http(s) URL to load. Internal/private hosts (localhost, "
            "10.x, 192.168.x, link-local) and non-http schemes are refused."
        ),
    )


class BrowserNavigateTool(BaseTool[BrowserNavigateArgs]):
    name = "browser_navigate"
    description = (
        "Open a URL in a shared headless Chromium page and wait for the DOM to "
        "load. Start here, then use the other browser_* tools (get_text, click, "
        "fill, screenshot, wait_for) on the SAME page. Unlike render_html the "
        "browser has live network access, but private/internal hosts are "
        "blocked. ~30s load budget."
    )
    args_model = BrowserNavigateArgs

    async def run(self, args: BrowserNavigateArgs) -> ToolResult:
        if self.ctx.browser_session is None:
            return ToolResult(content="browser session unavailable", is_error=True)
        check_navigable(args.url)  # raises ValueError → tool error on block
        page = await self.ctx.browser_session.ensure_page()
        response = await page.goto(
            args.url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
        )
        status = response.status if response is not None else 0
        log.info("browser navigated to %s (status %s)", page.url, status)
        return ToolResult(
            content=f"loaded {page.url} (HTTP {status})",
            data={"url": page.url, "status": status},
        )
