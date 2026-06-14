"""E2E: the owner /usage command relays Claude Code's own usage report.

given  the owner
when    they send /usage (in a DM or a group)
then    the bot replies with Claude Code's usage text — session and weekly
        limits — within the command-reply limit.

``/usage`` is intercepted by the harness before Claude: it shells out to a
short-lived ``claude --print /usage`` and forwards the result. It is owner-only;
the e2e tester account is configured as the owner, so it passes in DM and group.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.helpers import (
    MAX_USAGE_REPLY_S,
    Conversation,
    assert_reply_within,
    send_and_wait,
)

log = logging.getLogger(__name__)


async def _assert_usage_report(client: TelegramClient, convo: Conversation) -> None:
    # when the owner asks for a usage report
    reply = await send_and_wait(client, convo, "/usage")
    # then the bot relays Claude Code's usage text
    log.info("usage reply: %s", reply.text[:200])
    text = reply.text.lower()
    assert "usage" in text or "used" in text, (
        f"reply does not look like a usage report; was {reply.text!r}"
    )
    # ... and the relay stays within the usage-reply limit
    assert_reply_within(reply, MAX_USAGE_REPLY_S, "/usage")


async def test_usage_command_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_usage_report(tester_client, dm)


async def test_usage_command_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_usage_report(tester_client, group)
