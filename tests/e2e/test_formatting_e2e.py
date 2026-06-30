"""E2E: the bot's Markdown reply renders as real Telegram formatting — DM and group.

``markdown_to_telegram_html`` is unit-tested in isolation. This proves the HTML
it emits actually survives the real Bot API: a single reply asking for every
style comes back carrying the matching Telegram entities — bold, italic,
strikethrough, inline code, a code block, a link, and a quote. ``raw_text``
drops formatting, so the reply's ``entity_types`` is what we assert on.
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntityStrike,
    MessageEntityTextUrl,
)

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.config import MAX_TEXT_REPLY_S
from tests.e2e.support.models import Conversation

log = logging.getLogger(__name__)

#: One natural request that asks the bot to demonstrate every Markdown style in a
#: single message — phrased as a "show me how it looks" help request so the bot's
#: prompt-injection defense (which rejects blind "echo this") stays out of the way.
_SHOWCASE_REQUEST = (
    "I'm learning Telegram formatting. In one single message, show me each style "
    "so I can see how it looks, using Markdown: a word in **bold**, a word in "
    "*italic*, a word in ~~strikethrough~~, an `inline code` span, a fenced code "
    "block, a link written as [example](https://example.com), and a one-line "
    "quote that starts with `>`."
)

#: Each Markdown style the request asks for, paired with the Telegram entity its
#: converted HTML must produce on the message the tester receives.
_EXPECTED_ENTITIES = {
    "bold": MessageEntityBold,
    "italic": MessageEntityItalic,
    "strikethrough": MessageEntityStrike,
    "inline code": MessageEntityCode,
    "code block": MessageEntityPre,
    "link": MessageEntityTextUrl,
    "quote": MessageEntityBlockquote,
}


async def _assert_formats(client: TelegramClient, convo: Conversation) -> None:
    # warm-up turn pays the one-time startup cost; not measured
    await send_and_wait(client, convo, "Hello, are you there?")

    reply = await send_and_wait(client, convo, _SHOWCASE_REQUEST, timeout=120)
    log.info(
        "formatting reply: first=%.2fs complete=%.2fs entities=%s",
        reply.t_first_s,
        reply.t_complete_s,
        sorted(reply.entity_types),
    )

    assert reply.text.strip(), "bot sent no reply content"
    for style, entity in _EXPECTED_ENTITIES.items():
        assert entity.__name__ in reply.entity_types, (
            f"bot's reply has no {style} ({entity.__name__}); "
            f"got entities {sorted(reply.entity_types)}; reply text {reply.text!r}"
        )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "formatting")


@pytest.mark.smoke
async def test_bot_formats_reply_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """The bot's reply renders every Markdown style as Telegram formatting in a DM.

    given  a warm bot and a request to demonstrate every formatting style
    when   the tester asks in a DM
    then   the reply carries the bold, italic, strikethrough, inline-code,
           code-block, link, and quote entities within MAX_TEXT_REPLY_S.
    """
    await _assert_formats(tester_client, dm)


async def test_bot_formats_reply_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot's reply renders every Markdown style as Telegram formatting in a group.

    given  a warm bot and a request to demonstrate every formatting style
    when   the tester asks in a group
    then   the reply carries the bold, italic, strikethrough, inline-code,
           code-block, link, and quote entities within MAX_TEXT_REPLY_S.
    """
    await _assert_formats(tester_client, group)
