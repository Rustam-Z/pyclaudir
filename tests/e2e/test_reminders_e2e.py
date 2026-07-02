"""E2E: the bot schedules reminders, fires them, and seeds committed ones.

scheduled (DM and group, separate tests): "remind me in 30 minutes: TOKEN"
    -> a pending reminders row lands with a ~30-minute trigger_at.
fires (DM and group, separate tests, @slow): "remind me in 70 seconds: TOKEN"
    -> the bot delivers an unsolicited message with TOKEN within ~2.5 min, and
    the row flips to sent.
committed (default-reminders.json): a reminder declared in the file is seeded
    at startup with a ``committed:`` auto_seed_key, and its recurring fire
    (@slow) delivers to the owner while the row stays pending (never sent).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within, assert_within
from tests.e2e.support.client import send_and_wait, wait_for_message
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import reminder_rows
from tests.e2e.support.config import MAX_REMINDER_FIRE_S, MAX_REMINDER_REPLY_S
from tests.e2e.support.waits import measured, wait_until


async def _assert_scheduled(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    token = new_sentinel("REMIND")
    reply = await send_and_wait(
        client,
        convo,
        f"Set a reminder for 30 minutes from now with this exact text: {token}.",
    )
    assert_reply_within(reply, MAX_REMINDER_REPLY_S, "reminder")
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


async def _assert_fires(sut: Sut, client: TelegramClient, convo: Conversation) -> None:
    token = new_sentinel("FIRE")

    reply = await send_and_wait(
        client,
        convo,
        f"Set a reminder for 70 seconds from now with this exact text: {token}.",
    )
    assert_reply_within(reply, MAX_REMINDER_REPLY_S, "reminder")

    seen, elapsed = await measured(
        wait_for_message(client, convo, token, timeout=MAX_REMINDER_FIRE_S)
    )
    assert token in seen, f"reminder {token!r} never fired; saw {seen!r}"
    assert_within(elapsed, MAX_REMINDER_FIRE_S, "reminder fire")

    # the delivered reminder's row must flip from pending to sent
    sent = await wait_until(
        lambda: [r for r in reminder_rows(sut.db_path, token) if r["status"] == "sent"]
    )
    assert sent, f"reminder {token!r} row was not marked sent"


@pytest.mark.smoke
async def test_reminder_is_scheduled_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """The bot schedules a 30-minute reminder in a DM.

    given  the owner
    when   they ask for a reminder 30 minutes out in a DM
    then   a pending row lands with a ~30-minute trigger, replied within
           MAX_REMINDER_REPLY_S.
    """
    await _assert_scheduled(hamroh_sut, tester_client, dm)


@pytest.mark.smoke
async def test_reminder_is_scheduled_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot schedules a 30-minute reminder in a group.

    given  the owner
    when   they ask for a reminder 30 minutes out in a group
    then   a pending row lands with a ~30-minute trigger, replied within
           MAX_REMINDER_REPLY_S.
    """
    await _assert_scheduled(hamroh_sut, tester_client, group)


@pytest.mark.slow
@pytest.mark.smoke
async def test_reminder_fires_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """A scheduled reminder fires and is marked sent in a DM.

    given  the owner
    when   they ask for a reminder ~70 seconds out in a DM
    then   the bot delivers it within MAX_REMINDER_FIRE_S and the row flips to sent.
    """
    await _assert_fires(hamroh_sut, tester_client, dm)


@pytest.mark.slow
@pytest.mark.smoke
async def test_reminder_fires_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """A scheduled reminder fires and is marked sent in a group.

    given  the owner
    when   they ask for a reminder ~70 seconds out in a group
    then   the bot delivers it within MAX_REMINDER_FIRE_S and the row flips to sent.
    """
    await _assert_fires(hamroh_sut, tester_client, group)


# ---------------------------------------------------------------------------
# committed reminders declared in default-reminders.json
# ---------------------------------------------------------------------------


@pytest.mark.smoke
async def test_default_reminder_is_seeded(
    default_reminders_sut: tuple[Sut, str, str],
) -> None:
    """A reminder declared in default-reminders.json is seeded at startup.

    given  a default-reminders.json holding one recurring reminder
    when   the bot boots with it
    then   a pending reminders row lands, tagged with a committed: auto_seed_key
           and carrying the declared cron.
    """
    sut, token, _ = default_reminders_sut

    rows = await wait_until(lambda: reminder_rows(sut.db_path, token))

    assert rows, f"no committed reminder row was seeded for {token!r}"
    row = rows[0]
    # An every-minute committed reminder can be claimed (pending -> processing)
    # by the poll loop the instant it comes due, so a mid-flight read is valid.
    assert row["status"] in ("pending", "processing"), (
        f"unexpected status {row['status']!r}"
    )
    assert row["auto_seed_key"] and row["auto_seed_key"].startswith("committed:"), (
        f"a committed reminder must carry a committed: seed key, "
        f"got {row['auto_seed_key']!r}"
    )
    assert row["cron_expr"] == "* * * * *", (
        f"the declared cron must be stored on the row, got {row['cron_expr']!r}"
    )


@pytest.mark.smoke
async def test_disabled_default_reminder_is_not_seeded(
    default_reminders_sut: tuple[Sut, str, str],
) -> None:
    """A reminder with "enabled": false is skipped at startup.

    given  a default-reminders.json holding one enabled and one disabled reminder
    when   the bot boots and reconciles them in a single pass
    then   once the enabled reminder's row exists, the disabled one has no row —
           it was never seeded.
    """
    sut, token, disabled_token = default_reminders_sut

    # Wait on the enabled reminder: its row proves the reconcile pass ran, so the
    # disabled reminder's absence below is a settled fact, not a race.
    await wait_until(lambda: reminder_rows(sut.db_path, token))

    assert reminder_rows(sut.db_path, disabled_token) == [], (
        "a reminder with 'enabled': false must never be seeded"
    )


@pytest.mark.slow
@pytest.mark.smoke
async def test_default_reminder_fires(
    default_reminders_sut: tuple[Sut, str, str],
    tester_client: TelegramClient,
    dm: Conversation,
) -> None:
    """The seeded recurring reminder fires to the owner and stays pending.

    given  a booted bot with a committed every-minute reminder for the owner
    when   a poll cycle reaches its trigger time
    then   the bot delivers the reminder text to the owner within
           MAX_REMINDER_FIRE_S, and no row is marked sent (recurring, not one-shot).
    """
    sut, token, _ = default_reminders_sut

    seen, elapsed = await measured(
        wait_for_message(tester_client, dm, token, timeout=MAX_REMINDER_FIRE_S)
    )

    assert token in seen, f"committed reminder {token!r} never fired; saw {seen!r}"
    assert_within(elapsed, MAX_REMINDER_FIRE_S, "committed reminder fire")

    rows = reminder_rows(sut.db_path, token)
    assert rows and all(r["status"] != "sent" for r in rows), (
        "a recurring committed reminder must return to pending, never 'sent'"
    )
