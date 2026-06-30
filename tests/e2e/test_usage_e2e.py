"""E2E: the owner /usage command relays Claude Code's own usage report.

``/usage`` is owner-only and intercepted by the harness before Claude: it
shells out to a short-lived ``claude --print /usage`` and forwards the result.
The e2e tester account is configured as the owner, so it passes in DM and group.
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.models import Conversation
from tests.e2e.support.config import MAX_TEXT_REPLY_S

log = logging.getLogger(__name__)


async def _assert_usage_report(client: TelegramClient, convo: Conversation) -> None:
    reply = await send_and_wait(client, convo, "/usage")
    log.info("usage reply: %s", reply.text[:200])
    text = reply.text.lower()
    assert "usage" in text or "used" in text, (
        f"reply does not look like a usage report; was {reply.text!r}"
    )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "/usage")


@pytest.mark.smoke
async def test_usage_command_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """/usage in a DM relays Claude Code's usage report.

    given  the owner
    when   they send /usage in a DM
    then   the bot replies with Claude Code's usage text within MAX_TEXT_REPLY_S.
    """
    await _assert_usage_report(tester_client, dm)


async def test_usage_command_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """/usage in a group relays Claude Code's usage report.

    given  the owner
    when   they send /usage in a group
    then   the bot replies with Claude Code's usage text within MAX_TEXT_REPLY_S.
    """
    await _assert_usage_report(tester_client, group)
