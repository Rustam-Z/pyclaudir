"""E2E: owner access-management commands — /allow, /deny, /policy.

Owner-only commands that mutate the shared SUT's access.json, so every test
uses a throwaway dummy id (never the real owner/group) and restores the
original state in a ``finally`` block, leaving the bot usable for the next test.
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from hamroh.access import AccessConfig, load_access
from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import expect_silence, send_and_wait
from tests.e2e.support.config import MAX_TEXT_REPLY_S, E2EConfig
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut, set_access
from tests.e2e.support.models import Conversation

log = logging.getLogger(__name__)

# Throwaway ids that are neither the owner nor the test group, so adding or
# removing them can never lock the tester out of the shared SUT.
_DUMMY_ALLOW_DM = 988800001
_DUMMY_ALLOW_GROUP = 988800002
_DUMMY_DENY_DM = 988800003
_DUMMY_DENY_GROUP = 988800004


# --- /allow --------------------------------------------------------------


async def _assert_allow_adds(
    client: TelegramClient, convo: Conversation, sut: Sut, dummy: int
) -> None:
    try:
        reply = await send_and_wait(client, convo, f"/allow user {dummy}")
        assert_reply_within(reply, MAX_TEXT_REPLY_S, "/allow")
        assert "added" in reply.text.lower(), f"no add ack: {reply.text!r}"
        users = load_access(sut.access_path).allowed_users
        assert dummy in users, f"{dummy} not persisted to allowlist: {users}"
    finally:
        # always restore: drop the dummy so the allowlist is left clean
        await send_and_wait(client, convo, f"/deny user {dummy}")


@pytest.mark.slow
async def test_allow_command_dm(
    tester_client: TelegramClient, dm: Conversation, hamroh_sut: Sut
) -> None:
    """/allow in a DM adds a user to the allowlist.

    given  the owner
    when   they allow a dummy user in a DM
    then   the bot acks within MAX_TEXT_REPLY_S and the id is persisted to access.json (state restored after).
    """
    await _assert_allow_adds(tester_client, dm, hamroh_sut, _DUMMY_ALLOW_DM)


async def test_allow_command_group(
    tester_client: TelegramClient, group: Conversation, hamroh_sut: Sut
) -> None:
    """/allow in a group adds a user to the allowlist.

    given  the owner
    when   they allow a dummy user in a group
    then   the bot acks within MAX_TEXT_REPLY_S and the id is persisted to access.json (state restored after).
    """
    await _assert_allow_adds(tester_client, group, hamroh_sut, _DUMMY_ALLOW_GROUP)


@pytest.mark.smoke
async def test_group_access_commands_take_effect_group(
    tester_client: TelegramClient,
    group: Conversation,
    group_id: int,
    hamroh_sut: Sut,
    e2e_config: E2EConfig,
) -> None:
    """/deny group then /allow group actually flip the bot's acceptance.

    given  the test group on the allowlist
    when   the owner denies it via /deny group <id> from the DM
    then   a message in the group is ignored (no reply)
    when   the owner re-allows it via /allow group <id> from the DM
    then   a message in the group gets a reply within MAX_TEXT_REPLY_S
    (original access restored afterwards).

    Commands are sent from the DM because a denied group can no longer issue
    them. The owner DM stays authorized throughout.
    """
    # the DM addresses the bot directly; its entity is the group's reply sender.
    bot = group.reply_from
    dm = Conversation(chat=bot, reply_from=bot)
    # arrange: snapshot to restore, then pin a deterministic allowlist baseline
    # so the gate decision is unambiguous (open/owner_only would hide the deny).
    owner, gid = e2e_config.owner_id, group_id
    original = load_access(hamroh_sut.access_path)
    set_access(
        hamroh_sut,
        AccessConfig("allowlist", allowed_users=[owner], allowed_chats=[gid]),
    )
    try:
        # when the owner denies the group via command
        ack = await send_and_wait(tester_client, dm, f"/deny group {gid}")
        assert "removed" in ack.text.lower(), f"no deny ack: {ack.text!r}"

        # then a group message is no longer accepted (bot stays silent)
        token = new_sentinel("ACCESS-MANAGEMENT-DENY")
        silent = await expect_silence(tester_client, group, f"hi {token}")
        assert not silent, (
            f"denied group should be ignored; got {[m.raw_text for m in silent]!r}"
        )

        # when the owner re-allows the group via command
        ack = await send_and_wait(tester_client, dm, f"/allow group {gid}")
        assert "added" in ack.text.lower(), f"no allow ack: {ack.text!r}"

        # then a group message is accepted again and answered promptly
        token = new_sentinel("ACCESS-MANAGEMENT-ALLOW")
        reply = await send_and_wait(tester_client, group, f"hi {token}")
        assert reply.text, "re-allowed group got no reply"
        assert_reply_within(reply, MAX_TEXT_REPLY_S, "group after re-allow")
    finally:
        # always restore the shared SUT's original access
        set_access(hamroh_sut, original)


# --- /deny ---------------------------------------------------------------


async def _assert_deny_removes(
    client: TelegramClient, convo: Conversation, sut: Sut, dummy: int
) -> None:
    # arrange: put the dummy on the allowlist so there is something to remove
    await send_and_wait(client, convo, f"/allow user {dummy}")
    try:
        reply = await send_and_wait(client, convo, f"/deny user {dummy}")
        assert_reply_within(reply, MAX_TEXT_REPLY_S, "/deny")
        assert "removed" in reply.text.lower(), f"no remove ack: {reply.text!r}"
        users = load_access(sut.access_path).allowed_users
        assert dummy not in users, f"{dummy} still in allowlist: {users}"
    finally:
        # idempotent safety net in case the assertion left it allowed
        await send_and_wait(client, convo, f"/deny user {dummy}")


@pytest.mark.smoke
async def test_deny_command_dm(
    tester_client: TelegramClient, dm: Conversation, hamroh_sut: Sut
) -> None:
    """/deny in a DM removes a user from the allowlist.

    given  the owner and a dummy user on the allowlist
    when   they deny the dummy user in a DM
    then   the bot acks within MAX_TEXT_REPLY_S and the id is gone from access.json (state restored after).
    """
    await _assert_deny_removes(tester_client, dm, hamroh_sut, _DUMMY_DENY_DM)


async def test_deny_command_group(
    tester_client: TelegramClient, group: Conversation, hamroh_sut: Sut
) -> None:
    """/deny in a group removes a user from the allowlist.

    given  the owner and a dummy user on the allowlist
    when   they deny the dummy user in a group
    then   the bot acks within MAX_TEXT_REPLY_S and the id is gone from access.json (state restored after).
    """
    await _assert_deny_removes(tester_client, group, hamroh_sut, _DUMMY_DENY_GROUP)


# --- /policy -------------------------------------------------------------


async def _assert_policy_sets(
    client: TelegramClient, convo: Conversation, sut: Sut
) -> None:
    # capture the current policy so it can be restored afterwards
    original = load_access(sut.access_path).policy
    try:
        reply = await send_and_wait(client, convo, "/policy open")
        assert_reply_within(reply, MAX_TEXT_REPLY_S, "/policy")
        assert "open" in reply.text.lower(), f"no policy ack: {reply.text!r}"
        assert load_access(sut.access_path).policy == "open", "policy not persisted"
    finally:
        # always restore the original policy for the shared SUT
        await send_and_wait(client, convo, f"/policy {original}")


@pytest.mark.smoke
async def test_policy_command_dm(
    tester_client: TelegramClient, dm: Conversation, hamroh_sut: Sut
) -> None:
    """/policy in a DM switches the allowlist policy.

    given  the owner
    when   they switch the policy to open in a DM
    then   the bot acks within MAX_TEXT_REPLY_S and the new policy is persisted to access.json (original restored after).
    """
    await _assert_policy_sets(tester_client, dm, hamroh_sut)


async def test_policy_command_group(
    tester_client: TelegramClient, group: Conversation, hamroh_sut: Sut
) -> None:
    """/policy in a group switches the allowlist policy.

    given  the owner
    when   they switch the policy to open in a group
    then   the bot acks within MAX_TEXT_REPLY_S and the new policy is persisted to access.json (original restored after).
    """
    await _assert_policy_sets(tester_client, group, hamroh_sut)


# --- missing arguments: every command replies with usage, mutates nothing ---


async def _assert_usage_on_missing_args(
    client: TelegramClient, convo: Conversation, sut: Sut, command: str
) -> None:
    # arrange: snapshot the full access state so we can prove nothing changed
    before = load_access(sut.access_path)

    # act: send the bare command with no arguments
    reply = await send_and_wait(client, convo, command)

    # assert: usage hint, fast, and the access state is untouched
    assert_reply_within(reply, MAX_TEXT_REPLY_S, command)
    assert "usage" in reply.text.lower(), f"no usage hint for {command}: {reply.text!r}"
    after = load_access(sut.access_path)
    assert after.policy == before.policy, f"{command} changed policy: {after.policy}"
    assert after.allowed_users == before.allowed_users, f"{command} changed users"
    assert after.allowed_chats == before.allowed_chats, f"{command} changed chats"


async def test_allow_without_args_dm(
    tester_client: TelegramClient, dm: Conversation, hamroh_sut: Sut
) -> None:
    """/allow with no arguments shows usage and changes nothing, in a DM.

    given  the owner
    when   they send a bare /allow in a DM
    then   the bot replies with a usage hint within MAX_TEXT_REPLY_S and access.json is unchanged.
    """
    await _assert_usage_on_missing_args(tester_client, dm, hamroh_sut, "/allow")


async def test_allow_without_args_group(
    tester_client: TelegramClient, group: Conversation, hamroh_sut: Sut
) -> None:
    """/allow with no arguments shows usage and changes nothing, in a group.

    given  the owner
    when   they send a bare /allow in a group
    then   the bot replies with a usage hint within MAX_TEXT_REPLY_S and access.json is unchanged.
    """
    await _assert_usage_on_missing_args(tester_client, group, hamroh_sut, "/allow")


async def test_deny_without_args_dm(
    tester_client: TelegramClient, dm: Conversation, hamroh_sut: Sut
) -> None:
    """/deny with no arguments shows usage and changes nothing, in a DM.

    given  the owner
    when   they send a bare /deny in a DM
    then   the bot replies with a usage hint within MAX_TEXT_REPLY_S and access.json is unchanged.
    """
    await _assert_usage_on_missing_args(tester_client, dm, hamroh_sut, "/deny")


async def test_deny_without_args_group(
    tester_client: TelegramClient, group: Conversation, hamroh_sut: Sut
) -> None:
    """/deny with no arguments shows usage and changes nothing, in a group.

    given  the owner
    when   they send a bare /deny in a group
    then   the bot replies with a usage hint within MAX_TEXT_REPLY_S and access.json is unchanged.
    """
    await _assert_usage_on_missing_args(tester_client, group, hamroh_sut, "/deny")


async def test_policy_without_args_dm(
    tester_client: TelegramClient, dm: Conversation, hamroh_sut: Sut
) -> None:
    """/policy with no arguments shows usage and changes nothing, in a DM.

    given  the owner
    when   they send a bare /policy in a DM
    then   the bot replies with a usage hint within MAX_TEXT_REPLY_S and access.json is unchanged.
    """
    await _assert_usage_on_missing_args(tester_client, dm, hamroh_sut, "/policy")


async def test_policy_without_args_group(
    tester_client: TelegramClient, group: Conversation, hamroh_sut: Sut
) -> None:
    """/policy with no arguments shows usage and changes nothing, in a group.

    given  the owner
    when   they send a bare /policy in a group
    then   the bot replies with a usage hint within MAX_TEXT_REPLY_S and access.json is unchanged.
    """
    await _assert_usage_on_missing_args(tester_client, group, hamroh_sut, "/policy")
