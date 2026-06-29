"""Warm Chromium manager (headless by default) shared across render + browser calls.

Launching Playwright's node driver plus a fresh Chromium per render costs
~0.6-1.4s of pure overhead (issue #64). This keeps ONE Chromium process
warm for the bot's lifetime and hands out a fresh, isolated ``context()``
per render so renders never share cookies/cache/state.

Only the *context* is closed after each render; the browser stays up. A
crashed/disconnected browser is detected via ``is_connected`` and
relaunched on the next render. One browser only — no pool.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..base import ArgsT, BaseTool, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        ViewportSize,
    )

log = logging.getLogger(__name__)

#: Viewport for the interactive browser session (separate from per-render
#: viewports which are caller-supplied). A common desktop size.
_SESSION_VIEWPORT: "ViewportSize" = {"width": 1280, "height": 800}

#: Cap on how long a ``close()``/``stop()`` may take. A wedged context or
#: driver otherwise blocks the caller here.
_CLOSE_TIMEOUT_S = 5.0


class BrowserManager:
    """Owns a single warm Chromium process; serializes (re)launch.

    Lazily launches on first ``context()`` (or eagerly via ``warm()``),
    stays alive, relaunches if Chromium dies, and is torn down once via
    ``close()`` at shutdown. All launch/relaunch goes through ``_lock`` so
    two concurrent renders can't double-launch.
    """

    def __init__(self, *, headless: bool = True) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._headless = headless
        self._lock = asyncio.Lock()

    async def warm(self) -> None:
        """Launch the browser ahead of the first render.

        Errors are logged, not raised — a failed warm-up (e.g. chromium
        not installed) must not break startup; the first real render
        retries the launch and surfaces the error to the caller.
        """
        try:
            await self._ensure_browser()
        except Exception as exc:
            log.warning("browser warm-up failed: %s: %s", type(exc).__name__, exc)

    @asynccontextmanager
    async def context(
        self, *, viewport: ViewportSize, java_script_enabled: bool = True
    ) -> AsyncIterator[BrowserContext]:
        """Yield a fresh isolated context; close it (not the browser) after.

        Every render gets its own ``new_context`` so no cookies/localStorage/
        cache bleed between renders. The ``finally`` closes the context with a
        bounded budget; the warm browser survives regardless.
        """
        browser = await self._ensure_browser()
        ctx = await browser.new_context(
            viewport=viewport, java_script_enabled=java_script_enabled
        )
        try:
            yield ctx
        finally:
            await _safe_close(ctx, "render context")

    async def open_context(
        self, *, viewport: ViewportSize, java_script_enabled: bool = True
    ) -> BrowserContext:
        """Return a fresh isolated context the CALLER must close.

        Unlike :meth:`context`, this does not auto-close — used by the
        long-lived :class:`BrowserSession`, which keeps one context alive
        across many tool calls and closes it at shutdown. Same warm
        Chromium backs both, so no second browser is launched.
        """
        browser = await self._ensure_browser()
        return await browser.new_context(
            viewport=viewport, java_script_enabled=java_script_enabled
        )

    async def close(self) -> None:
        """Tear down browser + driver. Idempotent — shutdown calls it, and a
        prior crash may already have nulled them."""
        async with self._lock:
            await self._drop_dead_browser()

    async def _ensure_browser(self) -> Browser:
        """Return a connected browser, launching/relaunching as needed.

        Serialized by ``_lock``: the first caller launches; concurrent
        callers wait, then see the live browser. A disconnected browser
        (Chromium crashed) is dropped and relaunched.
        """
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            await self._drop_dead_browser()
            from playwright.async_api import async_playwright  # heavy; local

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            log.info("warm chromium launched (headless=%s)", self._headless)
            return self._browser

    async def _drop_dead_browser(self) -> None:
        """Release a dead/stale browser + driver before relaunch or shutdown.
        Best effort — errors are logged only, never raised."""
        await _safe_close(self._browser, "browser")
        await _safe_stop(self._pw)
        self._browser = None
        self._pw = None


async def _safe_close(
    obj: Any, label: str, *, timeout: float = _CLOSE_TIMEOUT_S
) -> None:
    """Close a browser/context with a bounded budget; log, never raise.

    Cleanup edge: the caller wants any original exception preserved, so a
    hung or failing ``close()`` is logged and swallowed.
    """
    if obj is None:
        return
    try:
        await asyncio.wait_for(obj.close(), timeout=timeout)
    except (asyncio.TimeoutError, Exception) as exc:
        log.warning("%s.close hung/failed: %s: %s", label, type(exc).__name__, exc)


async def _safe_stop(pw: Any) -> None:
    """Stop the Playwright driver; log, never raise."""
    if pw is None:
        return
    try:
        await pw.stop()
    except Exception as exc:
        log.warning("playwright.stop failed: %s: %s", type(exc).__name__, exc)


class BrowserSession:
    """One long-lived browser context + page shared across ``browser_*`` tools.

    Render hands out a fresh context per call, but interactive automation needs
    the SAME page to survive between separate tool invocations (navigate, then
    click/fill/get_text/screenshot). This holds that page — lazily created on
    the first navigate from the manager's warm Chromium, isolated in its own
    context (no operator profile, satisfying issue #38). Closed once at
    shutdown; the warm browser itself is untouched.
    """

    def __init__(self, manager: BrowserManager) -> None:
        self._manager = manager
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    async def ensure_page(self) -> Page:
        """Return the live page, creating the context + page on first use.

        Serialized by ``_lock`` so two concurrent tool calls can't open two
        contexts. A page closed by a crash is dropped and recreated.
        """
        async with self._lock:
            live = self._live_page()
            if live is not None:
                return live
            self._context = await self._manager.open_context(viewport=_SESSION_VIEWPORT)
            self._context.on("page", self._adopt)  # follow popups / new tabs
            self._page = await self._context.new_page()
            return self._page

    def require_page(self) -> Page:
        """Return the open page, or raise telling the caller to navigate first."""
        page = self._live_page()
        if page is None:
            raise RuntimeError("no page open — call browser_navigate first")
        return page

    async def reset(self) -> None:
        """Drop the context + page so the next navigate starts clean (fresh
        cookies/state). Same teardown as :meth:`close`; named for the tool."""
        await self.close()

    async def close(self) -> None:
        """Close the session context (not the warm browser). Idempotent."""
        await _safe_close(self._context, "browser session context")
        self._context = None
        self._page = None

    def _adopt(self, page: Page) -> None:
        """Make a newly-opened tab (popup / ``target=_blank``) the active page,
        so the next tool call acts on what the user's click just opened."""
        self._page = page

    def _live_page(self) -> Page | None:
        """The current page if open, else the most recent still-open tab in the
        context (a popup the active page spawned), else None."""
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._context is not None:
            for page in reversed(self._context.pages):
                if not page.is_closed():
                    self._page = page
                    return page
        return None


class BrowserSessionTool(BaseTool[ArgsT]):
    """Base for ``browser_*`` tools that act on an already-open page.

    Provides ``_require_page`` — fetch the shared session's live page or raise
    a clear error the MCP wrapper surfaces to the model — and ``_miss``, the
    uniform non-error result for an expected selector miss. ``run`` stays
    abstract, so tool auto-discovery skips this class.
    """

    def _require_page(self) -> Page:
        session = self.ctx.browser_session
        if session is None:
            raise RuntimeError("browser session unavailable")
        return session.require_page()

    @staticmethod
    def _miss(action: str, selector: str) -> ToolResult:
        """Non-fatal result for an expected selector miss/timeout.

        Probing for a selector that isn't there is normal browser control flow,
        not a tool malfunction. Returning this (instead of raising) lets the
        agent adapt — and, crucially, keeps the cc tool-error circuit breaker
        from killing the turn over a routine miss.
        """
        return ToolResult(
            content=(
                f"{action}: no element matching {selector!r} appeared in time "
                f"(it may not be on this page — try a different selector)"
            )
        )


async def try_selector(op: Any) -> tuple[bool, Any]:
    """Await a Playwright selector coroutine, classifying a not-found timeout.

    Returns ``(True, value)`` on success, or ``(False, None)`` when the op
    times out because the selector never appeared/became actionable — an
    expected miss the caller turns into a ``_miss`` result. Any other error
    (browser crash, protocol failure) propagates and rightly counts as a real
    tool error. The Playwright import stays lazy, matching ``BrowserManager``.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    try:
        return True, await op
    except PlaywrightTimeoutError:
        return False, None
