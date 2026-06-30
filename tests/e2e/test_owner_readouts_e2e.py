"""E2E: owner read-only status commands — /health, /audit, /access.

Owner-only readouts (the e2e tester account is configured as owner). Each
command is read-only (no state change), so there is no cleanup, and each is
exercised in both a DM and a group.
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.config import MAX_TEXT_REPLY_S, E2EConfig
from tests.e2e.support.models import Conversation

log = logging.getLogger(__name__)


async def _assert_readout(
    client: TelegramClient, convo: Conversation, command: str, *markers: str
) -> None:
    """Send ``command`` and assert its reply is prompt and contains ``markers``."""
    reply = await send_and_wait(client, convo, command)
    assert_reply_within(reply, MAX_TEXT_REPLY_S, command)
    text = reply.text.lower()
    for marker in markers:
        assert marker in text, f"{command} reply missing {marker!r}: {reply.text!r}"


# --- /health -------------------------------------------------------------


@pytest.mark.smoke
async def test_health_command_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """/health in a DM returns the health readout.

    given  the owner
    when   they send /health in a DM
    then   the bot replies with the health readout within MAX_TEXT_REPLY_S.
    """
    await _assert_readout(tester_client, dm, "/health", "health", "status")


async def test_health_command_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """/health in a group returns the health readout.

    given  the owner
    when   they send /health in a group
    then   the bot replies with the health readout within MAX_TEXT_REPLY_S.
    """
    await _assert_readout(tester_client, group, "/health", "health", "status")


# --- /audit --------------------------------------------------------------


@pytest.mark.smoke
async def test_audit_command_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """/audit in a DM returns the audit readout.

    given  the owner
    when   they send /audit in a DM
    then   the bot replies with the audit readout within MAX_TEXT_REPLY_S.
    """
    await _assert_readout(tester_client, dm, "/audit", "audit", "memory footprint")


async def test_audit_command_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """/audit in a group returns the audit readout.

    given  the owner
    when   they send /audit in a group
    then   the bot replies with the audit readout within MAX_TEXT_REPLY_S.
    """
    await _assert_readout(tester_client, group, "/audit", "audit", "memory footprint")


# --- /access -------------------------------------------------------------


async def _assert_access_report(
    client: TelegramClient, convo: Conversation, cfg: E2EConfig
) -> None:
    reply = await send_and_wait(client, convo, "/access")
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "/access")
    assert "policy" in reply.text.lower(), f"no policy line: {reply.text!r}"
    assert str(cfg.owner_id) in reply.text, f"owner id missing: {reply.text!r}"


@pytest.mark.smoke
async def test_access_command_dm(
    tester_client: TelegramClient, dm: Conversation, e2e_config: E2EConfig
) -> None:
    """/access in a DM names the policy and owner id.

    given  the owner
    when   they send /access in a DM
    then   the bot replies with the policy and owner id within MAX_TEXT_REPLY_S.
    """
    await _assert_access_report(tester_client, dm, e2e_config)


async def test_access_command_group(
    tester_client: TelegramClient, group: Conversation, e2e_config: E2EConfig
) -> None:
    """/access in a group names the policy and owner id.

    given  the owner
    when   they send /access in a group
    then   the bot replies with the policy and owner id within MAX_TEXT_REPLY_S.
    """
    await _assert_access_report(tester_client, group, e2e_config)
