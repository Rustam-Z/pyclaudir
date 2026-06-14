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

from tests.e2e.support.harness import Sut, message_rows
from tests.e2e.support.helpers import (
    MAX_COMMAND_REPLY_S,
    MAX_TEXT_REPLY_S,
    Conversation,
    assert_reply_within,
    expect_silence,
    new_sentinel,
    recall_prompt,
    send_and_wait,
)


async def _assert_paused_drops(
    client: TelegramClient, sut: Sut, dm: Conversation, target: Conversation
) -> None:
    try:
        # given the owner pauses the bot from the DM
        paused = await send_and_wait(client, dm, "/pause")
        assert_reply_within(paused, MAX_COMMAND_REPLY_S, "pause ack")
        assert "paus" in paused.text.lower(), f"no pause ack; got {paused.text!r}"

        # when a message arrives in the target chat while paused
        token = new_sentinel("DROPPED")
        replies = await expect_silence(client, target, f"hello {token}", within=8)

        # then it is not processed (no reply) ...
        assert not replies, (
            f"expected silence while paused; got {[m.raw_text for m in replies]!r}"
        )
        # ... and it never reached the messages table (DB stays clean) ...
        rows = message_rows(sut.db_path, token)
        assert not rows, f"paused message leaked into DB: {[dict(r) for r in rows]!r}"
        # ... and the bot is still running: /health answers and reports paused
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
        # given the bot was paused then resumed
        await send_and_wait(client, dm, "/pause")
        resumed = await send_and_wait(client, dm, "/resume")
        assert_reply_within(resumed, MAX_COMMAND_REPLY_S, "resume ack")
        assert "resum" in resumed.text.lower(), f"no resume ack; got {resumed.text!r}"

        # when a normal message arrives in the target chat
        question, token = recall_prompt()
        reply = await send_and_wait(client, target, question)

        # then it is processed and answered promptly (CC stayed warm)
        assert_reply_within(reply, MAX_TEXT_REPLY_S, "post-resume reply")
        assert token in reply.text, f"resumed reply missing {token!r}: {reply.text!r}"
    finally:
        # idempotent safety net in case an assertion left the bot paused
        await send_and_wait(client, dm, "/resume")


@pytest.mark.slow
async def test_paused_drops_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_paused_drops(tester_client, pyclaudir_sut, dm, dm)


@pytest.mark.slow
async def test_paused_drops_group(
    pyclaudir_sut: Sut,
    tester_client: TelegramClient,
    dm: Conversation,
    group: Conversation,
) -> None:
    await _assert_paused_drops(tester_client, pyclaudir_sut, dm, group)


async def test_resumed_processes_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_resumed_processes(tester_client, dm, dm)


async def test_resumed_processes_group(
    tester_client: TelegramClient, dm: Conversation, group: Conversation
) -> None:
    await _assert_resumed_processes(tester_client, dm, group)
