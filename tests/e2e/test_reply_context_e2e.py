"""E2E: the bot captures reply linkage and carries context across turns.

Proves two conversational properties in both a DM and a group: an inbound
reply records its parent (``reply_to_id``), and a fact stated in one turn is
recalled in a later turn from live context — distinct from the disk-backed
memory test.
"""

from __future__ import annotations

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within, assert_within
from tests.e2e.support.client import send, send_and_wait
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import reply_info
from tests.e2e.support.config import MAX_TEXT_REPLY_S
from tests.e2e.support.waits import measured, wait_until


async def _assert_reply_linkage(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    parent = new_sentinel("PARENT")
    child = new_sentinel("CHILD")

    first = await send(client, convo, f"Topic note: {parent}.")
    await send(client, convo, f"Question {child} about the note.", reply_to=first.id)

    # matched by token text — Bot-API message_ids differ from Telethon's
    row, elapsed = await measured(wait_until(lambda: reply_info(sut.db_path, child)))
    assert row is not None, f"no inbound row for reply {child!r}"
    assert row["reply_to_id"] is not None, f"reply linkage not captured for {child!r}"
    assert parent in (row["reply_to_text"] or ""), (
        f"reply linked to wrong parent; reply_to_text={row['reply_to_text']!r}"
    )
    assert_within(elapsed, MAX_TEXT_REPLY_S, "reply linkage")


async def _assert_context(client: TelegramClient, convo: Conversation) -> None:
    token = new_sentinel("CTX")
    await send_and_wait(
        client, convo, f"For this chat, my reference number is {token}."
    )
    reply = await send_and_wait(client, convo, "What is my reference number?")
    assert token in reply.text, (
        f"bot lost context for {token!r}; reply was {reply.text!r}"
    )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "context recall")


@pytest.mark.smoke
async def test_reply_linkage_is_captured_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """A reply in a DM records its parent on the inbound row.

    given  the tester
    when   they reply to an earlier message in a DM
    then   the inbound row carries reply_to_id back to the parent within MAX_TEXT_REPLY_S.
    """
    await _assert_reply_linkage(hamroh_sut, tester_client, dm)


@pytest.mark.smoke
async def test_reply_linkage_is_captured_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """A reply in a group records its parent on the inbound row.

    given  the tester
    when   they reply to an earlier message in a group
    then   the inbound row carries reply_to_id back to the parent within MAX_TEXT_REPLY_S.
    """
    await _assert_reply_linkage(hamroh_sut, tester_client, group)


@pytest.mark.smoke
async def test_context_carries_across_turns_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """A fact stated earlier is recalled later in a DM.

    given  a fact stated in one turn
    when   the tester asks about it in a later turn in a DM
    then   the bot recalls it from context within MAX_TEXT_REPLY_S.
    """
    await _assert_context(tester_client, dm)


@pytest.mark.smoke
async def test_context_carries_across_turns_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """A fact stated earlier is recalled later in a group.

    given  a fact stated in one turn
    when   the tester asks about it in a later turn in a group
    then   the bot recalls it from context within MAX_TEXT_REPLY_S.
    """
    await _assert_context(tester_client, group)
