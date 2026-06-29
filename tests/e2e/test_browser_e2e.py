"""E2E: the bot drives a real headless browser and sends back a screenshot.

Two scenarios, each in a DM and a group:

* **Google Images (realistic):** search Google, open the Images tab, screenshot
  the page, send it. The agent picks the selectors — the most environment-
  sensitive test (headless Google can show consent / bot walls), so it's the
  "does it work in the wild" check.
* **Click + screenshot (deterministic):** open a stable Wikipedia article,
  *click* a known in-article link, then screenshot the landed page and send it.
  Exercises the click → follow → screenshot → send chain on a reliable target.

Both assert the OUTCOME (a photo arrived) AND that the specific tools ran
(`browser_navigate` + `browser_screenshot`, plus `browser_click`), so the test
fails if the agent shortcuts the flow instead of really driving the browser.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_within
from tests.e2e.support.client import has_photo, send_and_wait_until
from tests.e2e.support.config import MAX_BROWSER_REPLY_S
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import new_png_files, tool_calls_since

log = logging.getLogger(__name__)

_GOOGLE_REQUEST = (
    "Open google.com, search for cute kittens, open the Images tab, take a "
    "screenshot of the image results, and send it to me as a photo."
)
_WIKI_REQUEST = (
    "Open the Wikipedia page en.wikipedia.org/wiki/Cat, then click the link to "
    "the 'Felidae' article — click it, don't type the URL. On that page, take a "
    "screenshot and send it to me as a photo."
)


async def _assert_drives_browser(
    sut: Sut,
    client: TelegramClient,
    convo: Conversation,
    request: str,
    *,
    require_tools: tuple[str, ...],
) -> None:
    """Given a browser task, when asked, then a photo arrives, the required
    tools ran, a screenshot PNG was written, and the turn stays in budget."""
    # Arrange: a time marker so we only see THIS turn's tool calls + files.
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    before = time.time()

    # Act: ask the bot to perform the browser flow. A browser turn emits several
    # progress messages ("on it…", "searching…") before the image — so we wait
    # up to MAX_BROWSER_REPLY_S for the message that actually carries the photo,
    # ignoring the chatter in between, rather than stopping at the first reply.
    reply = await send_and_wait_until(
        client, convo, request, until=has_photo, timeout=MAX_BROWSER_REPLY_S
    )

    tools = [r["tool_name"] for r in tool_calls_since(sut.db_path, since)]
    log.info(
        "browser flow: time-to-photo=%.1fs msgs=%d tools=%s",
        reply.t_complete_s,
        len(reply.chunks),
        tools,
    )

    # Assert: an image came back (within the wait budget; else media_kind is None).
    assert reply.media_kind == "photo", (
        f"no photo within {MAX_BROWSER_REPLY_S:.0f}s; "
        f"got {len(reply.chunks)} message(s), last text {reply.text!r}"
    )
    # Assert: the flow genuinely used the browser (no WebFetch shortcut).
    for tool in require_tools:
        assert tool in tools, f"expected {tool} to run; tools were {tools}"
    # Assert: an image file was produced on disk.
    pngs = new_png_files(sut.renders_dir, before)
    assert pngs, f"no new image PNG appeared in {sut.renders_dir}"
    # Assert: the whole flow finished within the browser budget.
    assert_within(reply.t_complete_s, MAX_BROWSER_REPLY_S, "browser flow")


# ---------------------------------------------------------------------------
# Google Images — realistic, agent-driven
# ---------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.slow
async def test_bot_google_images_screenshot_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """Bot opens Google Images and sends a screenshot of the results.

    given  a request to open Google Images and screenshot it
    when   the tester asks in a DM
    then   a photo arrives, browser_navigate + browser_screenshot ran, a PNG
           lands in data/renders/, and the turn completes within MAX_BROWSER_REPLY_S.
    """
    await _assert_drives_browser(
        hamroh_sut,
        tester_client,
        dm,
        _GOOGLE_REQUEST,
        require_tools=("browser_navigate", "browser_screenshot"),
    )


@pytest.mark.slow
async def test_bot_google_images_screenshot_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """Bot opens Google Images and sends a screenshot of the results.

    given  a request to open Google Images and screenshot it
    when   the tester asks in a group
    then   a photo arrives, browser_navigate + browser_screenshot ran, a PNG
           lands in data/renders/, and the turn completes within MAX_BROWSER_REPLY_S.
    """
    await _assert_drives_browser(
        hamroh_sut,
        tester_client,
        group,
        _GOOGLE_REQUEST,
        require_tools=("browser_navigate", "browser_screenshot"),
    )


# ---------------------------------------------------------------------------
# Click + screenshot — deterministic, stable target
# ---------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.slow
async def test_bot_clicks_link_and_screenshots_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """Bot clicks an in-article link, then screenshots and sends the page.

    given  a request to click a Wikipedia link and screenshot the landed page
    when   the tester asks in a DM
    then   a photo arrives, browser_navigate + browser_click + browser_screenshot
           ran, a PNG lands in data/renders/, and the turn stays in budget.
    """
    await _assert_drives_browser(
        hamroh_sut,
        tester_client,
        dm,
        _WIKI_REQUEST,
        require_tools=("browser_navigate", "browser_click", "browser_screenshot"),
    )


@pytest.mark.slow
async def test_bot_clicks_link_and_screenshots_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """Bot clicks an in-article link, then screenshots and sends the page.

    given  a request to click a Wikipedia link and screenshot the landed page
    when   the tester asks in a group
    then   a photo arrives, browser_navigate + browser_click + browser_screenshot
           ran, a PNG lands in data/renders/, and the turn stays in budget.
    """
    await _assert_drives_browser(
        hamroh_sut,
        tester_client,
        group,
        _WIKI_REQUEST,
        require_tools=("browser_navigate", "browser_click", "browser_screenshot"),
    )
