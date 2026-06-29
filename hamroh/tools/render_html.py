"""``render_html`` — render an HTML snippet to a PNG via headless Chromium.

Use this when the user needs something visually structured that Telegram
markdown can't represent: tables of any width, charts (Chart.js, D3 — but
inline the lib bytes; network is blocked), formatted comparisons/diffs.

Output lands under ``data/renders/`` with a unique filename. Pair with
``telegram_send_photo`` to actually deliver it to a chat.

Security: the headless browser has **all network access blocked** at the
route layer. Inline anything you need (CSS, JS libs, fonts). file:// is
also blocked — it would be a local-file-read primitive otherwise.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult
from .browser import BrowserManager

log = logging.getLogger(__name__)

#: Playwright's ``set_content(wait_until=...)`` accepts exactly these values.
WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


@dataclass(frozen=True)
class RenderRequest:
    """Everything :func:`_render_to_png` needs for one screenshot.

    ``allowed_hosts`` is internal (never an agent arg): ``None`` blocks all
    outbound traffic, a tuple allow-lists specific CDN hosts (``render_latex``
    passes KaTeX's). ``manager`` reuses the warm browser; ``None`` spins up a
    throwaway one for this call.
    """

    html: str
    width: int
    height: int
    out_path: Path
    allowed_hosts: tuple[str, ...] | None = None
    wait_until: WaitUntil = "domcontentloaded"
    manager: BrowserManager | None = None


#: Hard cap on viewport pixels. Past this point screenshots get heavy and
#: chromium gets sluggish. ``full_page=True`` captures beyond the viewport
#: but the viewport governs layout reflow.
_VIEWPORT_MIN = 200
_VIEWPORT_MAX = 4000
_DEFAULT_WIDTH = 800
_DEFAULT_HEIGHT = 600

#: Per-page render timeout (set_content + screenshot). Inside the browser.
_RENDER_TIMEOUT_MS = 15_000

#: Wall-clock budget for the whole render call (context + render). If we
#: exceed this, we cancel the inner coroutine, which fires the cleanup
#: path. Sized larger than the page timeout.
_WALL_CLOCK_S = 30.0


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


async def _render_to_png(req: RenderRequest) -> None:
    """Drive playwright to render ``req.html`` → ``req.out_path``.

    Pulled out so tests can monkey-patch a fake. Reuses the warm
    ``req.manager`` (or spins up a throwaway one when ``None``). Cleanup is
    layered: ``set_content`` bounds rendering inside the browser,
    ``manager.context()`` closes the per-render context on any path, and the
    whole coroutine is wrapped in ``asyncio.wait_for(_WALL_CLOCK_S)`` so a
    hung launch is cancelled (the context ``finally`` still fires) and a
    wedged Chromium is relaunched on the next render.
    """
    owns_manager = req.manager is None
    mgr = req.manager if req.manager is not None else BrowserManager()

    async def _do() -> None:
        async with mgr.context(
            viewport={"width": req.width, "height": req.height},
            java_script_enabled=True,
        ) as ctx:
            page = await ctx.new_page()
            await page.route("**/*", _route_filter(req.allowed_hosts))
            await page.set_content(
                req.html,
                wait_until=req.wait_until,
                timeout=_RENDER_TIMEOUT_MS,
            )
            await page.screenshot(path=str(req.out_path), full_page=True)

    try:
        await asyncio.wait_for(_do(), timeout=_WALL_CLOCK_S)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"render exceeded {_WALL_CLOCK_S}s wall-clock budget"
        ) from exc
    finally:
        if owns_manager:
            await mgr.close()


def _route_filter(allowed_hosts: tuple[str, ...] | None):
    """Build the Playwright route handler enforcing the network allow-list.

    ``None`` blocks everything; ``data:``/``about:``/``blob:`` URLs always
    pass (they don't hit the network); any other host must be allow-listed.
    """
    from urllib.parse import urlparse

    async def _route(route) -> None:
        if allowed_hosts is None:
            await route.abort()
            return
        scheme = route.request.url.split(":", 1)[0].lower()
        if scheme in ("data", "about", "blob"):
            await route.continue_()
            return
        host = urlparse(route.request.url).hostname or ""
        if host in allowed_hosts:
            await route.continue_()
        else:
            await route.abort()

    return _route


async def _render_or_error(req: RenderRequest) -> ToolResult | None:
    """Run the render; return an error ``ToolResult`` on failure, else ``None``.

    Cleans up any half-written file on a render failure so a later
    existence check can't mistake a corpse for output.
    """
    try:
        await _render_to_png(req)
        return None
    except ImportError as exc:
        return ToolResult(
            content=(
                "playwright not installed; run `uv sync` and "
                f"`playwright install chromium` on the host. ({exc})"
            ),
            is_error=True,
        )
    except Exception as exc:  # browser launch / render failure
        log.warning("render_html failed: %s: %s", type(exc).__name__, exc)
        try:
            if req.out_path.exists():
                req.out_path.unlink()
        except OSError:
            pass
        return ToolResult(
            content=f"render failed: {type(exc).__name__}: {exc}", is_error=True
        )


def _result_for(store, args: RenderHtmlArgs, out_path: Path) -> ToolResult:
    """Build the success (or empty-output) ``ToolResult`` after a render."""
    if not out_path.exists() or out_path.stat().st_size == 0:
        return ToolResult(content="render produced no output", is_error=True)
    relative = store.relative(out_path)
    size = out_path.stat().st_size
    log.info(
        "rendered html → %s (%d bytes, %dx%d)", relative, size, args.width, args.height
    )
    return ToolResult(
        content=(
            f"rendered to {relative} ({size} bytes). "
            f"Pass this path to telegram_send_photo to deliver it."
        ),
        data={
            "path": relative,
            "size_bytes": size,
            "width": args.width,
            "height": args.height,
        },
    )


class RenderHtmlTool(BaseTool[RenderHtmlArgs]):
    name = "render_html"
    description = (
        "Render an HTML snippet to a PNG via headless Chromium and save it "
        "under data/renders/. Returns the relative path; it is NOT sent — pair "
        "with telegram_send_photo to deliver it. Use for tables/charts/diffs "
        "Telegram markdown can't represent (it renders ASCII tables poorly); "
        "for math formulas use render_latex. Outbound network is BLOCKED in "
        "the browser, so inline any CSS/JS libs you need (Chart.js, D3, "
        "fonts). Renders synchronously with a ~30s budget."
    )
    args_model = RenderHtmlArgs

    async def run(self, args: RenderHtmlArgs) -> ToolResult:
        return await self._run(args)

    async def _run(
        self,
        args: RenderHtmlArgs,
        *,
        allowed_hosts: tuple[str, ...] | None = None,
        wait_until: WaitUntil = "domcontentloaded",
    ) -> ToolResult:
        """Internal entry point — companion tools (``render_latex``) call
        this directly to opt into a narrow CDN allow-list. Not exposed to
        the agent via the public ``args_model``.
        """
        store = self.ctx.render_store
        if store is None:
            return ToolResult(content="render store unavailable", is_error=True)

        out_path = store.allocate(args.title)
        req = RenderRequest(
            args.html,
            args.width,
            args.height,
            out_path,
            allowed_hosts=allowed_hosts,
            wait_until=wait_until,
            manager=self.ctx.browser_manager,
        )
        error = await _render_or_error(req)
        if error is not None:
            return error
        return _result_for(store, args, out_path)
