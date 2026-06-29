"""The ``browser_*`` tools: navigate, get_text, click, fill, screenshot,
wait_for, get_attribute, press_key, download, scroll, select_option, back,
reload, get_html, list, reset. Driven against a fake session/page (no real
Chromium), each test reads as given/when/then with a failure message on every
assert."""

from __future__ import annotations

from pathlib import Path

import pytest

from hamroh.storage.render import RenderStore
from hamroh.tools.base import ToolContext
from hamroh.tools.browser.browser_click import BrowserClickArgs, BrowserClickTool
from hamroh.tools.browser.browser_fill import BrowserFillArgs, BrowserFillTool
from hamroh.tools.browser.browser_get_text import (
    BrowserGetTextArgs,
    BrowserGetTextTool,
)
from hamroh.tools.browser.browser_navigate import (
    BrowserNavigateArgs,
    BrowserNavigateTool,
)
from hamroh.tools.browser.browser_screenshot import (
    BrowserScreenshotArgs,
    BrowserScreenshotTool,
)
from hamroh.tools.browser.browser_back import BrowserBackArgs, BrowserBackTool
from hamroh.tools.browser.browser_download import (
    BrowserDownloadArgs,
    BrowserDownloadTool,
    _decode_data_uri,
)
from hamroh.tools.browser.browser_get_attribute import (
    BrowserGetAttributeArgs,
    BrowserGetAttributeTool,
)
from hamroh.tools.browser.browser_get_html import (
    BrowserGetHtmlArgs,
    BrowserGetHtmlTool,
)
from hamroh.tools.browser.browser_list import BrowserListArgs, BrowserListTool
from hamroh.tools.browser.browser_press_key import (
    BrowserPressKeyArgs,
    BrowserPressKeyTool,
)
from hamroh.tools.browser.browser_reload import BrowserReloadArgs, BrowserReloadTool
from hamroh.tools.browser.browser_reset import BrowserResetArgs, BrowserResetTool
from hamroh.tools.browser.browser_scroll import BrowserScrollArgs, BrowserScrollTool
from hamroh.tools.browser.browser_select_option import (
    BrowserSelectOptionArgs,
    BrowserSelectOptionTool,
)
from hamroh.tools.browser.browser_wait_for import (
    BrowserWaitForArgs,
    BrowserWaitForTool,
)
from tests.unit.test_browser import FakeElement, FakePage

#: A public literal IP — passes the SSRF guard without any DNS lookup, so
#: navigate/download tests stay offline and deterministic.
_PUBLIC_URL = "http://8.8.8.8/"


class FakeSession:
    """Stands in for ``BrowserSession`` — hands the tools a fixed page."""

    def __init__(self, page: FakePage | None) -> None:
        self._page = page
        self.reset_called = False

    async def ensure_page(self) -> FakePage:
        assert self._page is not None
        return self._page

    def require_page(self) -> FakePage:
        if self._page is None:
            raise RuntimeError("no page open — call browser_navigate first")
        return self._page

    async def reset(self) -> None:
        self.reset_called = True


@pytest.fixture()
def page() -> FakePage:
    return FakePage()


def _ctx(page: FakePage | None, *, store: RenderStore | None = None) -> ToolContext:
    return ToolContext(browser_session=FakeSession(page), render_store=store)


# ---------------------------------------------------------------------------
# browser_navigate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_loads_url_and_reports_status(page: FakePage) -> None:
    # Given a navigate tool over a fresh session
    tool = BrowserNavigateTool(_ctx(page))

    # When it navigates to a public URL
    result = await tool.run(BrowserNavigateArgs(url=_PUBLIC_URL))

    # Then the page loaded and the status came back
    assert page.goto_calls == [_PUBLIC_URL], "navigate must goto the requested URL"
    assert "HTTP 200" in result.content, "navigate must report the load status"
    assert result.data == {"url": _PUBLIC_URL, "status": 200}, "structured data wrong"


@pytest.mark.asyncio
async def test_navigate_blocks_internal_url(page: FakePage) -> None:
    # Given a navigate tool
    tool = BrowserNavigateTool(_ctx(page))

    # When asked to open an internal host, Then it refuses before loading
    with pytest.raises(ValueError):
        await tool.run(BrowserNavigateArgs(url="http://127.0.0.1/admin"))
    assert page.goto_calls == [], "a blocked URL must never reach goto"


# ---------------------------------------------------------------------------
# browser_get_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_text_returns_page_text(page: FakePage) -> None:
    # Given a page with short text
    page.text = "the heading"
    tool = BrowserGetTextTool(_ctx(page))

    # When the whole body is read
    result = await tool.run(BrowserGetTextArgs())

    # Then the text comes back untruncated
    assert result.content == "the heading", "get_text must return the page text"


@pytest.mark.asyncio
async def test_get_text_truncates_huge_pages(page: FakePage) -> None:
    # Given a page far larger than the limit
    page.text = "x" * 50_000
    tool = BrowserGetTextTool(_ctx(page))

    # When read
    result = await tool.run(BrowserGetTextArgs())

    # Then it's truncated and says so
    assert "[truncated at 10000 chars]" in result.content, "must flag truncation"
    assert result.data == {"chars": 50_000}, "must report the true length"


# ---------------------------------------------------------------------------
# browser_click / browser_fill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_calls_through(page: FakePage) -> None:
    # Given a click tool, When it clicks a selector
    tool = BrowserClickTool(_ctx(page))
    await tool.run(BrowserClickArgs(selector="button.go"))

    # Then the page received the click
    assert page.clicks == ["button.go"], "click must reach the page"


@pytest.mark.asyncio
async def test_fill_calls_through(page: FakePage) -> None:
    # Given a fill tool, When it fills a field
    tool = BrowserFillTool(_ctx(page))
    await tool.run(BrowserFillArgs(selector="input[name=q]", value="cats"))

    # Then the page received the value
    assert page.fills == [("input[name=q]", "cats")], "fill must reach the page"


# ---------------------------------------------------------------------------
# browser_wait_for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_passes_selector_and_timeout(page: FakePage) -> None:
    # Given a wait tool, When it waits with a custom timeout
    tool = BrowserWaitForTool(_ctx(page))
    await tool.run(BrowserWaitForArgs(selector=".results", timeout_ms=5000))

    # Then the page was asked to wait for that selector and budget
    assert page.waits == [(".results", 5000)], "wait_for must forward selector+timeout"


# ---------------------------------------------------------------------------
# browser_screenshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_full_page_returns_image(
    page: FakePage, tmp_path: Path
) -> None:
    # Given a screenshot tool with a real render store
    store = RenderStore(tmp_path / "renders")
    store.ensure_root()
    tool = BrowserScreenshotTool(_ctx(page, store=store))

    # When it captures the full page
    result = await tool.run(BrowserScreenshotArgs())

    # Then a PNG was written and its PATH was returned (NOT image_path, which the
    # MCP wrapper would swallow — hiding the path telegram_send_photo needs).
    assert page.page_shots == 1, "full-page screenshot must hit page.screenshot"
    assert result.image_path is None, "must return the path, not an inline image"
    rel = result.data["path"]
    assert rel in result.content, "the saved path must be in the content"
    assert (store.resolve_path(rel)).exists(), "the screenshot file must exist on disk"


@pytest.mark.asyncio
async def test_screenshot_with_selector_captures_element(
    page: FakePage, tmp_path: Path
) -> None:
    # Given a screenshot tool with a real render store
    store = RenderStore(tmp_path / "renders")
    store.ensure_root()
    tool = BrowserScreenshotTool(_ctx(page, store=store))

    # When it captures a single element
    result = await tool.run(BrowserScreenshotArgs(selector="img.first"))

    # Then only that element was shot (not the whole page)
    assert page.element_shots == ["img.first"], "selector must capture the element"
    assert page.page_shots == 0, "selector mode must not full-page screenshot"
    assert result.image_path is None, "must return the path, not an inline image"
    assert result.data["path"] in result.content, "the saved path must be returned"


# ---------------------------------------------------------------------------
# pre-navigate guard shared by every non-navigate tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_error_before_navigate() -> None:
    # Given a session that has never navigated (no page)
    tool = BrowserGetTextTool(_ctx(None))

    # When a tool acts, Then it tells the caller to navigate first
    with pytest.raises(RuntimeError, match="browser_navigate first"):
        await tool.run(BrowserGetTextArgs())


# ---------------------------------------------------------------------------
# browser_get_attribute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_attribute_returns_value(page: FakePage) -> None:
    # Given a page whose <img> has a src
    page.attrs = {"src": "https://cdn/x.png"}
    tool = BrowserGetAttributeTool(_ctx(page))

    # When the src is read, Then the URL comes back
    result = await tool.run(BrowserGetAttributeArgs(selector="img", name="src"))
    assert result.content == "https://cdn/x.png", "must return the attribute value"


@pytest.mark.asyncio
async def test_get_attribute_reports_absent(page: FakePage) -> None:
    # Given a page where the attribute is missing
    page.attrs = {}
    tool = BrowserGetAttributeTool(_ctx(page))

    # When read, Then it says the attribute is absent
    result = await tool.run(BrowserGetAttributeArgs(selector="img", name="src"))
    assert "no attribute" in result.content, "missing attribute must be reported"


# ---------------------------------------------------------------------------
# browser_press_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_press_key_on_selector(page: FakePage) -> None:
    # Given a press tool, When Enter is pressed on a field
    tool = BrowserPressKeyTool(_ctx(page))
    await tool.run(BrowserPressKeyArgs(key="Enter", selector="input[name=q]"))

    # Then the page pressed the key on that field
    assert page.key_presses == [("input[name=q]", "Enter")], "must press on the field"


@pytest.mark.asyncio
async def test_press_key_on_focused_element(page: FakePage) -> None:
    # Given a press tool, When a key is pressed with no selector
    tool = BrowserPressKeyTool(_ctx(page))
    await tool.run(BrowserPressKeyArgs(key="Escape"))

    # Then it went to the focused element (keyboard, selector None)
    assert page.key_presses == [(None, "Escape")], "must press on focused element"


# ---------------------------------------------------------------------------
# browser_download
# ---------------------------------------------------------------------------


def test_decode_data_uri_base64() -> None:
    # A base64 data: URI decodes to its raw bytes.
    assert _decode_data_uri("data:text/plain;base64,SGk=") == b"Hi", "base64 decode"


@pytest.mark.asyncio
async def test_download_http_saves_and_returns_image(
    page: FakePage, tmp_path: Path
) -> None:
    # Given a download tool and a public image URL
    store = RenderStore(tmp_path / "renders")
    store.ensure_root()
    tool = BrowserDownloadTool(_ctx(page, store=store))

    # When it downloads the URL
    result = await tool.run(BrowserDownloadArgs(url=_PUBLIC_URL + "x.png"))

    # Then the bytes were fetched through the page and saved; the PATH is returned
    # (not image_path, which the MCP wrapper would swallow).
    assert page.requested == [_PUBLIC_URL + "x.png"], "must fetch via the page request"
    assert result.image_path is None, "must return the path, not an inline image"
    saved = store.resolve_path(result.data["path"])
    assert saved.exists(), "download must save the file"
    assert saved.read_bytes() == b"IMGBYTES", "saved bytes must match"
    assert result.data["path"] in result.content, "the saved path must be returned"


@pytest.mark.asyncio
async def test_download_blocks_internal_url(page: FakePage, tmp_path: Path) -> None:
    # Given a download tool
    store = RenderStore(tmp_path / "renders")
    store.ensure_root()
    tool = BrowserDownloadTool(_ctx(page, store=store))

    # When asked to download from an internal host, Then it refuses
    with pytest.raises(ValueError):
        await tool.run(BrowserDownloadArgs(url="http://169.254.169.254/meta"))
    assert page.requested == [], "a blocked URL must never be fetched"


# ---------------------------------------------------------------------------
# browser_scroll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scroll_by_pixels(page: FakePage) -> None:
    # Given a scroll tool, When scrolling the window
    tool = BrowserScrollTool(_ctx(page))
    await tool.run(BrowserScrollArgs(pixels=500))

    # Then the wheel was turned by that amount
    assert page.wheels == [(0, 500)], "must scroll the window by pixels"


@pytest.mark.asyncio
async def test_scroll_element_into_view(page: FakePage) -> None:
    # Given a scroll tool, When scrolling an element into view
    tool = BrowserScrollTool(_ctx(page))
    await tool.run(BrowserScrollArgs(selector=".target"))

    # Then that element was scrolled into view
    assert page.scrolled_into_view == [".target"], "must scroll the element into view"


# ---------------------------------------------------------------------------
# browser_select_option / back / reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_option_calls_through(page: FakePage) -> None:
    tool = BrowserSelectOptionTool(_ctx(page))
    await tool.run(BrowserSelectOptionArgs(selector="select#c", value="usd"))
    assert page.selects == [("select#c", "usd")], "select_option must reach the page"


@pytest.mark.asyncio
async def test_back_navigates_history(page: FakePage) -> None:
    tool = BrowserBackTool(_ctx(page))
    result = await tool.run(BrowserBackArgs())
    assert page.go_backs == 1, "back must call go_back"
    assert "https://prev/" in result.content, "back must report the landed URL"


@pytest.mark.asyncio
async def test_reload_refreshes(page: FakePage) -> None:
    tool = BrowserReloadTool(_ctx(page))
    await tool.run(BrowserReloadArgs())
    assert page.reloads == 1, "reload must call page.reload"


# ---------------------------------------------------------------------------
# browser_get_html / browser_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_html_returns_markup(page: FakePage) -> None:
    page.html = "<div>hi</div>"
    tool = BrowserGetHtmlTool(_ctx(page))
    result = await tool.run(BrowserGetHtmlArgs())
    assert result.content == "<div>hi</div>", "get_html must return the markup"


@pytest.mark.asyncio
async def test_list_enumerates_elements(page: FakePage) -> None:
    # Given a page with two links
    page.elements = [
        FakeElement("First", {"href": "/a"}),
        FakeElement("Second", {"href": "/b"}),
    ]
    tool = BrowserListTool(_ctx(page))

    # When listed, Then each element's text + href appears
    result = await tool.run(BrowserListArgs(selector="a"))
    assert "First" in result.content and "/a" in result.content, "must list text+href"
    assert result.data == {"count": 2}, "must report the match count"


# ---------------------------------------------------------------------------
# browser_reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_calls_session_reset() -> None:
    # Given a reset tool over a session
    ctx = _ctx(FakePage())
    tool = BrowserResetTool(ctx)

    # When reset runs, Then the session was reset
    result = await tool.run(BrowserResetArgs())
    assert ctx.browser_session.reset_called is True, "reset must drop the session"
    assert "navigate" in result.content, "reset must tell the user to navigate again"


# ---------------------------------------------------------------------------
# selector miss → non-error result (must NOT trip the cc tool-error breaker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_returns_miss_not_error_on_timeout(page: FakePage) -> None:
    # Given a page whose selector never appears (Playwright raises TimeoutError)
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    async def times_out(*_a, **_k) -> None:
        raise PlaywrightTimeoutError("Timeout 8000ms exceeded")

    page.wait_for_selector = times_out  # type: ignore[assignment]
    tool = BrowserWaitForTool(_ctx(page))

    # When the agent waits for a missing selector
    result = await tool.run(BrowserWaitForArgs(selector="img.nope", timeout_ms=8000))

    # Then it's a normal result the agent can react to — NOT a raised tool error
    # (a raise would feed the circuit breaker and kill the turn).
    assert result.is_error is False, "a selector miss must not be a tool error"
    assert "img.nope" in result.content, "the miss must name the selector"
