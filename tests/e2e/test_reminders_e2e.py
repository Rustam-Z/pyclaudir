"""E2E: the bot schedules reminders and fires them.

scheduled (DM and group, separate tests): "remind me in 30 minutes: TOKEN"
    -> a pending reminders row lands with a ~30-minute trigger_at.
fires (DM, @slow): "remind me in 70 seconds: TOKEN" -> the bot delivers an
    unsolicited message with TOKEN within ~2.5 min, and the row flips to sent.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.harness import Sut, reminder_rows
from tests.e2e.support.helpers import (
    MAX_REMINDER_FIRE_S,
    MAX_REMINDER_REPLY_S,
    Conversation,
    assert_reply_within,
    assert_within,
    measured,
    new_sentinel,
    send_and_wait,
    wait_for_message,
    wait_until,
)


async def _assert_scheduled(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    token = new_sentinel("REMIND")
    # when we ask for a reminder 30 minutes out
    reply = await send_and_wait(
        client,
        convo,
        f"Set a reminder for 30 minutes from now with this exact text: {token}.",
    )
    assert_reply_within(reply, MAX_REMINDER_REPLY_S, "reminder")
    # then a pending one-shot row lands with a ~30-minute trigger
    rows = await wait_until(lambda: reminder_rows(sut.db_path, token))
    assert rows, f"no reminders row for {token!r}"
    row = rows[0]
    assert row["status"] == "pending", f"unexpected status {row['status']!r}"
    trigger = datetime.strptime(row["trigger_at"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    minutes = (trigger - datetime.now(timezone.utc)).total_seconds() / 60
    assert 20 <= minutes <= 45, (
        f"trigger_at {row['trigger_at']} is {minutes:.1f} min out, expected ~30"
    )


async def test_reminder_is_scheduled_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_scheduled(pyclaudir_sut, tester_client, dm)


async def test_reminder_is_scheduled_group(
    pyclaudir_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_scheduled(pyclaudir_sut, tester_client, group)


@pytest.mark.slow
async def test_reminder_fires_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    token = new_sentinel("FIRE")

    # when we ask for a reminder ~70 seconds out
    reply = await send_and_wait(
        tester_client,
        dm,
        f"Set a reminder for 70 seconds from now with this exact text: {token}.",
    )
    assert_reply_within(reply, MAX_REMINDER_REPLY_S, "reminder")

    # then the bot delivers it within the fire limit ...
    seen, elapsed = await measured(
        wait_for_message(tester_client, dm, token, timeout=MAX_REMINDER_FIRE_S)
    )
    assert token in seen, f"reminder {token!r} never fired; saw {seen!r}"
    assert_within(elapsed, MAX_REMINDER_FIRE_S, "reminder fire")

    # ... and the row is marked sent
    sent = await wait_until(
        lambda: [
            r
            for r in reminder_rows(pyclaudir_sut.db_path, token)
            if r["status"] == "sent"
        ]
    )
    assert sent, f"reminder {token!r} row was not marked sent"
