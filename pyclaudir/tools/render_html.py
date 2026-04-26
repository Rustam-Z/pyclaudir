"""``render_html`` — render an HTML snippet to a PNG via headless Chromium.

Use this when the user needs something visually structured that Telegram
markdown can't represent: tables of any width, charts (Chart.js, D3 — but
inline the lib bytes; network is blocked), formatted comparisons/diffs.

Output lands under ``data/renders/`` with a unique filename. Pair with
``send_photo`` to actually deliver it to a chat.

Security: the headless browser has **all network access blocked** at the
route layer. Inline anything you need (CSS, JS libs, fonts). file:// is
also blocked — it would be a local-file-read primitive otherwise.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult

log = logging.getLogger(__name__)

#: Hard cap on viewport pixels. Past this point screenshots get heavy and
#: chromium gets sluggish. ``full_page=True`` captures beyond the viewport
#: but the viewport governs layout reflow.
_VIEWPORT_MIN = 200
_VIEWPORT_MAX = 4000
_DEFAULT_WIDTH = 800
_DEFAULT_HEIGHT = 600

#: Render timeout. Anything taking longer is almost certainly a script
#: with an infinite loop.
_RENDER_TIMEOUT_MS = 15_000


class RenderHtmlArgs(BaseModel):
    html: str = Field(
        min_length=1,
        description=(
            "Full HTML body to render. Inline all CSS/JS — outbound network "
            "is blocked. Wrap with <!DOCTYPE html><html><body>...</body>"
            "</html> for full control over fonts and viewport meta."
        ),
    )
    width: int = Field(
        default=_DEFAULT_WIDTH,
        ge=_VIEWPORT_MIN,
        le=_VIEWPORT_MAX,
        description="Viewport width in pixels (default 800).",
    )
    height: int = Field(
        default=_DEFAULT_HEIGHT,
        ge=_VIEWPORT_MIN,
        le=_VIEWPORT_MAX,
        description="Viewport height in pixels (default 600). Full page is captured regardless.",
    )
    title: str | None = Field(
        default=None,
        max_length=80,
        description="Optional human-readable label baked into the filename for easier identification.",
    )


async def _render_to_png(html: str, width: int, height: int, out_path: Path) -> None:
    """Drive playwright to render ``html`` → ``out_path``.

    Pulled out so tests can monkey-patch a fake without spinning up a
    real browser.
    """
    from playwright.async_api import async_playwright  # local import — heavy

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(
                viewport={"width": width, "height": height},
                java_script_enabled=True,
            )
            page = await ctx.new_page()

            async def _abort(route):  # block ALL outbound traffic
                await route.abort()

            await page.route("**/*", _abort)
            await page.set_content(
                html,
                wait_until="domcontentloaded",
                timeout=_RENDER_TIMEOUT_MS,
            )
            await page.screenshot(path=str(out_path), full_page=True)
        finally:
            await browser.close()


class RenderHtmlTool(BaseTool):
    name = "render_html"
    description = (
        "Render an HTML snippet to a PNG via headless Chromium and save it "
        "under data/renders/. Returns the relative path; pair with "
        "send_photo to deliver it to a chat. Use for tables/charts/diffs "
        "that Telegram markdown can't represent — Telegram doesn't render "
        "ASCII tables well. Outbound network is BLOCKED inside the browser, "
        "so inline any CSS/JS libs you need (Chart.js, D3, fonts)."
    )
    args_model = RenderHtmlArgs

    async def run(self, args: RenderHtmlArgs) -> ToolResult:
        store = self.ctx.render_store
        if store is None:
            return ToolResult(content="render store unavailable", is_error=True)

        out_path = store.allocate(args.title)
        try:
            await _render_to_png(args.html, args.width, args.height, out_path)
        except ImportError as exc:
            return ToolResult(
                content=(
                    "playwright not installed; run `uv sync` and "
                    "`playwright install chromium` on the host. "
                    f"({exc})"
                ),
                is_error=True,
            )
        except Exception as exc:  # browser launch / render failure
            log.warning("render_html failed: %s: %s", type(exc).__name__, exc)
            # Best-effort cleanup of any half-written file.
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            return ToolResult(
                content=f"render failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        if not out_path.exists() or out_path.stat().st_size == 0:
            return ToolResult(
                content="render produced no output",
                is_error=True,
            )

        relative = store.relative(out_path)
        size = out_path.stat().st_size
        log.info(
            "rendered html → %s (%d bytes, %dx%d)",
            relative, size, args.width, args.height,
        )
        return ToolResult(
            content=(
                f"rendered to {relative} ({size} bytes). "
                f"Pass this path to send_photo to deliver it."
            ),
            data={
                "path": relative,
                "size_bytes": size,
                "width": args.width,
                "height": args.height,
            },
        )
