"""E2E: the bot reacts to a message with an emoji — DM and group.

Mirrors the "emojis" scenario: the bot's telegram_add_reaction tool, proven by the
reaction actually showing up on the message via Telethon.
"""

from __future__ import annotations

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_within
from tests.e2e.support.client import send, wait_for_reaction
from tests.e2e.support.models import Conversation
from tests.e2e.support.config import MAX_TEXT_REPLY_S
from tests.e2e.support.waits import measured

_EMOJI = "👍"


async def _assert_reacts(client: TelegramClient, convo: Conversation) -> None:
    sent = await send(client, convo, f"React to this message with the {_EMOJI} emoji.")
    reacted, elapsed = await measured(
        wait_for_reaction(client, convo.chat, sent.id, _EMOJI)
    )
    assert reacted, f"bot did not react with {_EMOJI} to message {sent.id}"
    assert_within(elapsed, MAX_TEXT_REPLY_S, "reaction")


@pytest.mark.smoke
async def test_bot_reacts_with_emoji_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """Bot reacts with an emoji to a message.

    given  a message asking the bot to react with 👍
    when   the tester sends it in a DM
    then   the 👍 reaction appears on that message within MAX_TEXT_REPLY_S.
    """
    await _assert_reacts(tester_client, dm)


async def test_bot_reacts_with_emoji_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """Bot reacts with an emoji to a message.

    given  a message asking the bot to react with 👍
    when   the tester sends it in a group
    then   the 👍 reaction appears on that message within MAX_TEXT_REPLY_S.
    """
    await _assert_reacts(tester_client, group)
