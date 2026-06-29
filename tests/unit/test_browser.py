"""``BrowserManager`` — warm reuse, crash relaunch, context isolation,
idempotent close, and the bounded ``_safe_close`` helper. All fakes; no
real Chromium is spun up."""

from __future__ import annotations

import asyncio

import pytest

from pathlib import Path

from hamroh.tools.browser.browser import (
    BrowserManager,
    BrowserSession,
    BrowserSessionTool,
    _safe_close,
    try_selector,
)
from hamroh.tools.browser.ssrf import check_navigable

_VIEWPORT = {"width": 800, "height": 600}


# ---------------------------------------------------------------------------
# Fakes standing in for the playwright object graph
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakeApiResponse:
    """A page.request.get() result — body + ok/status."""

    def __init__(self, status: int = 200, body: bytes = b"IMGBYTES") -> None:
        self.status = status
        self._body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    async def body(self) -> bytes:
        return self._body


class FakeRequest:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    async def get(self, url: str, **_kw) -> FakeApiResponse:
        self._page.requested.append(url)
        return self._page.api_response


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    async def press(self, key: str) -> None:
        self._page.key_presses.append((None, key))


class FakeMouse:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    async def wheel(self, dx: int, dy: int) -> None:
        self._page.wheels.append((dx, dy))


class FakeElement:
    """An ElementHandle from query_selector_all."""

    def __init__(self, text: str = "", attrs: dict[str, str] | None = None) -> None:
        self._text = text
        self._attrs = attrs or {}

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)


class FakeLocator:
    """One element handle — screenshot + scroll are exercised."""

    def __init__(self, page: "FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    async def screenshot(self, *, path: str) -> None:
        self._page.element_shots.append(self._selector)
        Path(path).write_bytes(b"\x89PNG-element")

    async def scroll_into_view_if_needed(self, *, timeout: int) -> None:
        self._page.scrolled_into_view.append(self._selector)


class FakePage:
    """Stands in for a Playwright page across the browser_* tools."""

    def __init__(self) -> None:
        self.closed = False
        self.url = "about:blank"
        self.text = "hello world"
        self.html = "<html><body>hi</body></html>"
        self.attrs: dict[str, str | None] = {
            "href": "https://x/y",
            "src": "https://i/p",
        }
        self.elements: list[FakeElement] = []
        self.api_response = FakeApiResponse()
        self.goto_calls: list[str] = []
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.waits: list[tuple[str, int]] = []
        self.key_presses: list[tuple[str | None, str]] = []
        self.selects: list[tuple[str, str]] = []
        self.wheels: list[tuple[int, int]] = []
        self.scrolled_into_view: list[str] = []
        self.requested: list[str] = []
        self.go_backs = 0
        self.reloads = 0
        self.page_shots = 0
        self.element_shots: list[str] = []
        self.keyboard = FakeKeyboard(self)
        self.mouse = FakeMouse(self)
        self.request = FakeRequest(self)

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, **_kw) -> FakeResponse:
        self.goto_calls.append(url)
        self.url = url
        return FakeResponse(200)

    async def go_back(self, **_kw) -> None:
        self.go_backs += 1
        self.url = "https://prev/"

    async def reload(self, **_kw) -> None:
        self.reloads += 1

    async def inner_text(self, _selector: str, **_kw) -> str:
        return self.text

    async def inner_html(self, _selector: str, **_kw) -> str:
        return self.html

    async def content(self) -> str:
        return self.html

    async def click(self, selector: str, **_kw) -> None:
        self.clicks.append(selector)

    async def fill(self, selector: str, value: str, **_kw) -> None:
        self.fills.append((selector, value))

    async def press(self, selector: str, key: str, **_kw) -> None:
        self.key_presses.append((selector, key))

    async def select_option(self, selector: str, value: str, **_kw) -> list[str]:
        self.selects.append((selector, value))
        return [value]

    async def get_attribute(self, _selector: str, name: str, **_kw) -> str | None:
        return self.attrs.get(name)

    async def query_selector_all(self, _selector: str) -> list[FakeElement]:
        return self.elements

    async def wait_for_selector(self, selector: str, *, timeout: int) -> None:
        self.waits.append((selector, timeout))

    async def screenshot(self, *, path: str, **_kw) -> None:
        self.page_shots += 1
        Path(path).write_bytes(b"\x89PNG-page")

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


class FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self.pages: list[FakePage] = []
        self._page_handlers: list = []

    def on(self, event: str, handler) -> None:
        if event == "page":
            self._page_handlers.append(handler)

    async def new_page(self) -> FakePage:
        return self._spawn_page()

    def open_popup(self) -> FakePage:
        """Test helper: simulate a target=_blank / popup opening a new tab."""
        return self._spawn_page()

    def _spawn_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        for handler in self._page_handlers:
            handler(page)
        return page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.connected = True
        self.closed = False
        self.contexts: list[FakeContext] = []

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **_kw) -> FakeContext:
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browsers: list[FakeBrowser]) -> None:
        self._browsers = browsers
        self.launch_count = 0

    async def launch(self, **_kw) -> FakeBrowser:
        browser = self._browsers[self.launch_count]
        self.launch_count += 1
        return browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeContextManager:
    """Stands in for ``async_playwright()`` — supports ``.start()``."""

    def __init__(self, pw: FakePlaywright) -> None:
        self._pw = pw

    async def start(self) -> FakePlaywright:
        return self._pw


@pytest.fixture()
def fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeChromium, list[FakeBrowser]]:
    """Patch ``async_playwright`` so two launches yield two fresh browsers."""
    browsers = [FakeBrowser(), FakeBrowser()]
    chromium = FakeChromium(browsers)
    pw = FakePlaywright(chromium)
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakeContextManager(pw)
    )
    return chromium, browsers


# ---------------------------------------------------------------------------
# BrowserManager behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_browser_is_reused_across_renders(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a manager with a fake playwright backing it
    chromium, browsers = fake_playwright
    manager = BrowserManager()

    # When two renders each take a context
    async with manager.context(viewport=_VIEWPORT):
        pass
    async with manager.context(viewport=_VIEWPORT):
        pass

    # Then Chromium launched exactly once and each render got its own context
    assert chromium.launch_count == 1, "warm browser must be reused, not relaunched"
    assert len(browsers[0].contexts) == 2, "each render must get a fresh context"
    await manager.close()


@pytest.mark.asyncio
async def test_disconnected_browser_is_relaunched(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a manager whose browser has been used once
    chromium, browsers = fake_playwright
    manager = BrowserManager()
    async with manager.context(viewport=_VIEWPORT):
        pass

    # When the browser dies while idle and another render arrives
    browsers[0].connected = False
    async with manager.context(viewport=_VIEWPORT):
        pass

    # Then the dead browser is dropped and a fresh one is launched
    assert chromium.launch_count == 2, "a disconnected browser must be relaunched"
    assert browsers[0].closed is True, "the stale browser must be released first"
    await manager.close()


@pytest.mark.asyncio
async def test_context_is_closed_but_browser_kept_warm(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a manager
    _chromium, browsers = fake_playwright
    manager = BrowserManager()

    # When a render takes a context and exits
    async with manager.context(viewport=_VIEWPORT) as ctx:
        rendered_ctx = ctx

    # Then the per-render context is closed but the browser stays up
    assert rendered_ctx.closed is True, "the per-render context must close on exit"
    assert browsers[0].closed is False, "the warm browser must NOT close after a render"
    await manager.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a manager with a launched browser
    _chromium, browsers = fake_playwright
    manager = BrowserManager()
    async with manager.context(viewport=_VIEWPORT):
        pass

    # When close is called twice
    await manager.close()
    await manager.close()  # must not raise

    # Then the browser was torn down
    assert browsers[0].closed is True, "close must tear the browser down"


# ---------------------------------------------------------------------------
# _safe_close — bounded, non-raising cleanup helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_close_bounds_a_hanging_close() -> None:
    # Given an object whose close() never returns
    class Hanging:
        async def close(self) -> None:
            await asyncio.sleep(60)

    # When closed with a tiny budget, it returns instead of hanging
    await asyncio.wait_for(_safe_close(Hanging(), "x", timeout=0.05), timeout=2.0)


@pytest.mark.asyncio
async def test_safe_close_swallows_a_raising_close() -> None:
    # Given an object whose close() raises (already-dead browser)
    class Raising:
        async def close(self) -> None:
            raise RuntimeError("connection closed")

    # When closed, the error is logged and not propagated
    await _safe_close(Raising(), "x")


@pytest.mark.asyncio
async def test_safe_close_ignores_none() -> None:
    # A None target is a no-op (nothing to close yet).
    await _safe_close(None, "x")


# ---------------------------------------------------------------------------
# open_context — caller-owned context for the long-lived session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_context_does_not_auto_close(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a manager backed by a fake browser
    _chromium, browsers = fake_playwright
    manager = BrowserManager()

    # When a caller opens a session context
    ctx = await manager.open_context(viewport=_VIEWPORT)

    # Then it stays OPEN — the caller (the session) owns teardown
    assert ctx.closed is False, "open_context must not auto-close the context"
    assert browsers[0].closed is False, "the warm browser must stay up"
    await manager.close()


# ---------------------------------------------------------------------------
# BrowserSession — one live page reused across tool calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_page_creates_once_and_reuses(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a session over a fake browser
    chromium, _browsers = fake_playwright
    session = BrowserSession(BrowserManager())

    # When ensure_page is called twice
    first = await session.ensure_page()
    second = await session.ensure_page()

    # Then the same page is reused and only one context was opened
    assert first is second, "ensure_page must reuse the live page, not recreate it"
    assert chromium.launch_count == 1, "the warm browser must back the session"


@pytest.mark.asyncio
async def test_require_page_raises_before_navigate() -> None:
    # Given a session that has never navigated
    session = BrowserSession(BrowserManager())

    # When a tool asks for the page, Then it's told to navigate first
    with pytest.raises(RuntimeError, match="browser_navigate first"):
        session.require_page()


@pytest.mark.asyncio
async def test_session_close_closes_context_and_is_idempotent(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a session with an open page
    _chromium, _browsers = fake_playwright
    session = BrowserSession(BrowserManager())
    await session.ensure_page()
    context = session._context  # the live FakeContext the session owns

    # When close is called twice
    await session.close()
    await session.close()  # must not raise

    # Then the context was torn down and the page is forgotten
    assert context.closed is True, "session close must close the live context"
    with pytest.raises(RuntimeError, match="browser_navigate first"):
        session.require_page()


@pytest.mark.asyncio
async def test_session_adopts_popup_as_active_page(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a session with an open page
    _chromium, _browsers = fake_playwright
    session = BrowserSession(BrowserManager())
    first = await session.ensure_page()

    # When a click opens a new tab (popup / target=_blank)
    popup = session._context.open_popup()

    # Then the new tab becomes what the next tool acts on
    assert popup is not first, "the popup must be a distinct page"
    assert session.require_page() is popup, "a new tab must become the active page"


@pytest.mark.asyncio
async def test_require_page_falls_back_to_open_tab(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a session whose active tab is a popup that then closes
    _chromium, _browsers = fake_playwright
    session = BrowserSession(BrowserManager())
    first = await session.ensure_page()
    popup = session._context.open_popup()
    popup.closed = True

    # When a tool asks for the page, Then it falls back to the still-open tab
    assert session.require_page() is first, "must fall back to a still-open tab"


@pytest.mark.asyncio
async def test_reset_drops_the_page(
    fake_playwright: tuple[FakeChromium, list[FakeBrowser]],
) -> None:
    # Given a session with an open page
    _chromium, _browsers = fake_playwright
    session = BrowserSession(BrowserManager())
    await session.ensure_page()

    # When reset, Then the page is gone (navigate required again)
    await session.reset()
    with pytest.raises(RuntimeError, match="browser_navigate first"):
        session.require_page()


# ---------------------------------------------------------------------------
# check_navigable — the SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
    ],
)
def test_check_navigable_blocks_internal_targets(blocked_url: str) -> None:
    # Internal / non-web targets must be refused before any page load.
    with pytest.raises(ValueError):
        check_navigable(blocked_url)


def test_check_navigable_allows_public_http() -> None:
    # A public hostname passes the guard (DNS resolves to a global IP).
    check_navigable("https://example.com/path?q=1")


# ---------------------------------------------------------------------------
# try_selector / _miss — expected selector misses are NOT tool errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_selector_reports_timeout_as_miss() -> None:
    # Given a Playwright op that times out (selector never appeared)
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    async def times_out() -> None:
        raise PlaywrightTimeoutError("not found")

    # When run through the guard, Then it's flagged as a miss, not raised
    ok, value = await try_selector(times_out())
    assert ok is False and value is None, "a timeout must surface as (False, None)"


@pytest.mark.asyncio
async def test_try_selector_returns_value_on_success() -> None:
    # A successful op returns (True, value).
    async def succeeds() -> str:
        return "href-value"

    ok, value = await try_selector(succeeds())
    assert ok is True and value == "href-value", "success must return the value"


@pytest.mark.asyncio
async def test_try_selector_propagates_real_errors() -> None:
    # A genuine failure (not a selector miss) must still raise — it's a real
    # tool error that should reach the breaker.
    async def crashes() -> None:
        raise RuntimeError("browser disconnected")

    with pytest.raises(RuntimeError, match="disconnected"):
        await try_selector(crashes())


def test_miss_is_a_non_error_result() -> None:
    # _miss is a normal (non-error) result so the cc breaker ignores it.
    result = BrowserSessionTool._miss("click", "button.go")
    assert result.is_error is False, "a selector miss must not be a tool error"
    assert "button.go" in result.content, "the miss must name the selector"
