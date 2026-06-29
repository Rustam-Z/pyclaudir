"""E2E: the bot renders a diagram and sends it as a photo — DM and group.

The render-tool duration and the turn latency are logged — render is the
slowest tool path, so this is where the "diagram + speed" numbers come from.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_within
from tests.e2e.support.client import has_photo, send_and_wait_until
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import new_png_files, tool_calls_since
from tests.e2e.support.config import MAX_RENDER_REPLY_S

log = logging.getLogger(__name__)


async def _assert_renders(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    token = new_sentinel("DIAG")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    before = time.time()

    # The bot sometimes sends a text line before the photo, so wait for the
    # message that actually carries the photo rather than stopping at the first.
    reply = await send_and_wait_until(
        client,
        convo,
        f"Render a small HTML table containing the text {token} and send it to me "
        "as a photo.",
        until=has_photo,
        timeout=180,
    )

    render_ms = next(
        (
            r["duration_ms"]
            for r in tool_calls_since(sut.db_path, since)
            if r["tool_name"] == "render_html"
        ),
        None,
    )
    log.info(
        "render: time-to-photo=%.2fs render_html=%sms msgs=%d",
        reply.t_complete_s,
        render_ms,
        len(reply.chunks),
    )

    assert reply.media_kind == "photo", (
        f"no photo within {MAX_RENDER_REPLY_S:.0f}s; "
        f"got {len(reply.chunks)} message(s), last text {reply.text!r}"
    )
    pngs = new_png_files(sut.renders_dir, before)
    assert pngs, f"no new PNG appeared in {sut.renders_dir}"
    # Time to the photo (not to a text ack) within the render budget.
    assert_within(reply.t_complete_s, MAX_RENDER_REPLY_S, "render")


@pytest.mark.smoke
@pytest.mark.slow
async def test_bot_renders_and_sends_photo_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """Bot renders content as an image and sends it as a photo.

    given  a request to render content as an image
    when   the tester asks in a DM
    then   a photo arrives within MAX_RENDER_REPLY_S and a PNG lands in data/renders/.
    """
    await _assert_renders(hamroh_sut, tester_client, dm)


@pytest.mark.slow
async def test_bot_renders_and_sends_photo_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """Bot renders content as an image and sends it as a photo.

    given  a request to render content as an image
    when   the tester asks in a group
    then   a photo arrives within MAX_RENDER_REPLY_S and a PNG lands in data/renders/.
    """
    await _assert_renders(hamroh_sut, tester_client, group)
