"""E2E: the owner /kill command shuts the bot process down.

/kill SIGTERMs the process, so this runs against a throwaway ``killable_sut``
rather than the shared session bot. The reply ("Shutting down…") may or may not
land before the process dies, so the assertion first watches the process exit,
then probes the chat: a killed bot must not answer a follow-up message.
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.client import expect_silence, send
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.config import MAX_KILL_S
from tests.e2e.support.waits import wait_until

log = logging.getLogger(__name__)


async def _assert_kill_exits(
    client: TelegramClient, convo: Conversation, victim: Sut
) -> None:
    await send(client, convo, "/kill")
    # the reply may not arrive before the process dies, so watch the process
    exited = await wait_until(
        lambda: victim.proc.poll() is not None, timeout=MAX_KILL_S
    )
    assert exited, f"bot did not exit within {MAX_KILL_S:.0f}s of /kill"
    # prove it stays dead: a dead bot can't answer a follow-up message
    replies = await expect_silence(client, convo, "are you there?")
    assert not replies, f"killed bot still replied: {[m.raw_text for m in replies]}"


@pytest.mark.smoke
async def test_kill_command_dm(
    tester_client: TelegramClient, dm: Conversation, killable_sut: Sut
) -> None:
    """/kill shuts the bot process down from a DM.

    given  the owner and a throwaway bot
    when   they send /kill in a DM
    then   the bot process exits within MAX_KILL_S and stops answering.
    """
    await _assert_kill_exits(tester_client, dm, killable_sut)


async def test_kill_command_group(
    tester_client: TelegramClient, group: Conversation, killable_sut: Sut
) -> None:
    """/kill shuts the bot process down from a group.

    given  the owner and a throwaway bot
    when   they send /kill in a group
    then   the bot process exits within MAX_KILL_S and stops answering.
    """
    await _assert_kill_exits(tester_client, group, killable_sut)
