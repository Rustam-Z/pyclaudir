"""E2E: /pause and /resume — owner-only message muting, DM and group.

Four scenarios, one per chat type:

paused → not processed   the owner sends /pause; a message in the target chat
                         gets no reply, never lands in the messages table, and
                         the bot stays alive (/health still answers PAUSED).
resumed → processed      after /resume a normal message is answered again, and
                         the prompt latency proves the CC session stayed warm.

Owner control commands (/pause, /resume, /health) are always issued in the owner
DM — group command addressing is quirky (see test_memory_e2e.py). The pause
applies to all chats, so the `target` conversation (where the drop / reply is
verified) is the DM for one test and the group for the other.
"""

from __future__ import annotations

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import expect_silence, send_and_wait
from tests.e2e.support.data import new_sentinel, recall_prompt
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import message_rows
from tests.e2e.support.config import MAX_COMMAND_REPLY_S, MAX_TEXT_REPLY_S


async def _assert_paused_drops(
    client: TelegramClient, sut: Sut, dm: Conversation, target: Conversation
) -> None:
    try:
        paused = await send_and_wait(client, dm, "/pause")
        assert_reply_within(paused, MAX_COMMAND_REPLY_S, "pause ack")
        assert "paus" in paused.text.lower(), f"no pause ack; got {paused.text!r}"

        token = new_sentinel("DROPPED")
        replies = await expect_silence(client, target, f"hello {token}")

        assert not replies, (
            f"expected silence while paused; got {[m.raw_text for m in replies]!r}"
        )
        # the dropped message must never reach the messages table
        rows = message_rows(sut.db_path, token)
        assert not rows, f"paused message leaked into DB: {[dict(r) for r in rows]!r}"
        # the bot is still alive: /health answers and reports paused
        health = await send_and_wait(client, dm, "/health")
        assert_reply_within(health, MAX_COMMAND_REPLY_S, "health while paused")
        assert "paused" in health.text.lower(), f"/health not paused; {health.text!r}"
    finally:
        # always resume so the shared SUT is left usable for other tests
        await send_and_wait(client, dm, "/resume")


async def _assert_resumed_processes(
    client: TelegramClient, dm: Conversation, target: Conversation
) -> None:
    try:
        await send_and_wait(client, dm, "/pause")
        resumed = await send_and_wait(client, dm, "/resume")
        assert_reply_within(resumed, MAX_COMMAND_REPLY_S, "resume ack")
        assert "resum" in resumed.text.lower(), f"no resume ack; got {resumed.text!r}"

        question, token = recall_prompt()
        reply = await send_and_wait(client, target, question)

        # prompt latency proves the CC session stayed warm across the pause
        assert_reply_within(reply, MAX_TEXT_REPLY_S, "post-resume reply")
        assert token in reply.text, f"resumed reply missing {token!r}: {reply.text!r}"
    finally:
        # idempotent safety net in case an assertion left the bot paused
        await send_and_wait(client, dm, "/resume")


@pytest.mark.slow
async def test_paused_drops_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """A paused bot drops messages in a DM but stays alive.

    given  a bot paused via /pause in the owner DM
    when   a message arrives in the DM while paused
    then   it gets no reply, never reaches the DB, and /health answers paused
           within MAX_COMMAND_REPLY_S.
    """
    await _assert_paused_drops(tester_client, pyclaudir_sut, dm, dm)


@pytest.mark.slow
async def test_paused_drops_group(
    pyclaudir_sut: Sut,
    tester_client: TelegramClient,
    dm: Conversation,
    group: Conversation,
) -> None:
    """A paused bot drops messages in a group but stays alive.

    given  a bot paused via /pause in the owner DM
    when   a message arrives in the group while paused
    then   it gets no reply, never reaches the DB, and /health answers paused
           within MAX_COMMAND_REPLY_S.
    """
    await _assert_paused_drops(tester_client, pyclaudir_sut, dm, group)


async def test_resumed_processes_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """A resumed bot answers messages again in a DM.

    given  a bot paused then resumed via /resume in the owner DM
    when   a normal message arrives in the DM
    then   it is answered within MAX_TEXT_REPLY_S, proving the CC session stayed warm.
    """
    await _assert_resumed_processes(tester_client, dm, dm)


async def test_resumed_processes_group(
    tester_client: TelegramClient, dm: Conversation, group: Conversation
) -> None:
    """A resumed bot answers messages again in a group.

    given  a bot paused then resumed via /resume in the owner DM
    when   a normal message arrives in the group
    then   it is answered within MAX_TEXT_REPLY_S, proving the CC session stayed warm.
    """
    await _assert_resumed_processes(tester_client, dm, group)


@pytest.mark.slow
async def test_pause_resume_lifecycle_group(
    pyclaudir_sut: Sut,
    tester_client: TelegramClient,
    dm: Conversation,
    group: Conversation,
) -> None:
    """Pausing then resuming from the owner DM toggles group processing.

    given  the owner /pause from the DM
    when   a group message arrives while paused, then it is dropped (no reply,
           never reaches the DB); when the owner /resume and a normal group
           message arrives, then it is answered, proving the round-trip restores
           group processing within the command/text latency bounds.
    """
    try:
        paused = await send_and_wait(tester_client, dm, "/pause")
        assert_reply_within(paused, MAX_COMMAND_REPLY_S, "pause ack")
        assert "paus" in paused.text.lower(), f"no pause ack; got {paused.text!r}"

        token = new_sentinel("DROPPED")
        replies = await expect_silence(tester_client, group, f"hello {token}")
        assert not replies, (
            f"expected silence while paused; got {[m.raw_text for m in replies]!r}"
        )
        rows = message_rows(pyclaudir_sut.db_path, token)
        assert not rows, f"paused message leaked into DB: {[dict(r) for r in rows]!r}"

        resumed = await send_and_wait(tester_client, dm, "/resume")
        assert_reply_within(resumed, MAX_COMMAND_REPLY_S, "resume ack")
        assert "resum" in resumed.text.lower(), f"no resume ack; got {resumed.text!r}"

        question, recall_token = recall_prompt()
        reply = await send_and_wait(tester_client, group, question)
        assert_reply_within(reply, MAX_TEXT_REPLY_S, "post-resume reply")
        assert recall_token in reply.text, (
            f"resumed reply missing {recall_token!r}: {reply.text!r}"
        )
    finally:
        # idempotent safety net in case an assertion left the bot paused
        await send_and_wait(tester_client, dm, "/resume")
