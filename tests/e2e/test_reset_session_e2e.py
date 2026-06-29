"""E2E: the owner /reset_session command respawns Claude with a fresh context.

The reset is safe on the shared SUT: chat history (SQLite) and memories
(markdown) are preserved by design; only the in-context Claude session is
dropped and respawned.
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.data import recall_prompt
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import current_cc_session_id
from tests.e2e.support.config import MAX_RESET_REPLY_S, MAX_TEXT_REPLY_S

log = logging.getLogger(__name__)


async def _assert_reset_and_recover(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    # the session id before the reset
    before_id = current_cc_session_id(sut.cc_logs_dir)

    # reset the session
    reply = await send_and_wait(client, convo, "/reset_session", timeout=60)
    assert_reply_within(reply, MAX_RESET_REPLY_S, "/reset_session")
    text = reply.text.lower()
    assert "session" in text and ("cleared" in text or "fresh" in text), (
        f"reply does not look like a reset ack: {reply.text!r}"
    )

    # the next message is still answered (proving recovery) and runs on the
    # respawned session, which writes its new session-id capture file
    question, token = recall_prompt()
    recovered = await send_and_wait(client, convo, question)
    assert_reply_within(recovered, MAX_TEXT_REPLY_S, "post-reset reply")
    assert token in recovered.text, (
        f"engine did not answer after reset; reply was {recovered.text!r}"
    )

    # the session id after the reset must be a different one
    after_id = current_cc_session_id(sut.cc_logs_dir)
    log.info("cc session id: %s -> %s", before_id, after_id)
    assert after_id is not None, "no cc session id after reset"
    assert after_id != before_id, (
        f"session id must change after /reset_session, but stayed {before_id!r}"
    )


@pytest.mark.smoke
async def test_reset_session_command_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """/reset_session respawns the engine with a NEW session id and recovers (DM).

    given  the owner and the current Claude Code session id
    when   they send /reset_session in a DM
    then   the bot acks within MAX_RESET_REPLY_S, the next message is answered,
           and the cc session id has changed.
    """
    await _assert_reset_and_recover(hamroh_sut, tester_client, dm)


async def test_reset_session_command_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """/reset_session respawns the engine with a NEW session id and recovers (group).

    given  the owner and the current Claude Code session id
    when   they send /reset_session in a group
    then   the bot acks within MAX_RESET_REPLY_S, the next message is answered,
           and the cc session id has changed.
    """
    await _assert_reset_and_recover(hamroh_sut, tester_client, group)
