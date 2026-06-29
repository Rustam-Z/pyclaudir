"""E2E: a turn that outlives the status interval gets a progress heartbeat.

The worker promises that a long task is never silent: every
``HAMROH_STATUS_INTERVAL_SECONDS`` it tells the waiting chat it is still
working (see ``worker._status_heartbeat``). The interval is squeezed to
``STATUS_SUT_INTERVAL_S`` on a dedicated bot (the ``status_sut`` fixture), so
only this test sees it — every other test keeps the production interval.

To cross it deterministically and cheaply, the bot is parked in a single
``browser_wait_for`` on a selector that never appears — a "light sleep" that
blocks the turn server-side for its full timeout (a selector miss is expected,
not a tool error). The heartbeat fires mid-wait; the bot then sends a unique
done-marker, which both ends the turn (so it can't bleed into the next test on
the shared bot) and tells the test the run is over.
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_within
from tests.e2e.support.client import send_and_watch_status
from tests.e2e.support.config import (
    MAX_STATUS_PING_S,
    MAX_STATUS_TURN_S,
    STATUS_SUT_INTERVAL_S,
)
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut, wait_for_engine_idle
from tests.e2e.support.models import Conversation

log = logging.getLogger(__name__)

#: Tolerance below the interval for the first ping — guards against a bug that
#: fires the heartbeat at turn start instead of after the interval.
_PING_FLOOR_SLACK_S = 5.0

#: A valid CSS selector that will never match — the bot's wait runs its full
#: timeout, holding the turn open without doing any real work.
_MISSING_SELECTOR = "#hamroh-e2e-heartbeat-never"


def _light_sleep_request(done_marker: str) -> str:
    """A request that parks the turn in one ~15s browser wait (comfortably over
    the 10s interval), then signs off with ``done_marker`` so the test knows the
    turn ended."""
    return (
        "Do exactly these steps with the browser, in order:\n"
        "1. Open https://example.com\n"
        f"2. Call browser_wait_for for the CSS selector '{_MISSING_SELECTOR}' "
        "with the full 15000 ms timeout. That element does not exist — that is "
        "expected; let the wait run its whole duration and do not retry it.\n"
        f"3. Once the wait has finished, send me one message that says exactly: "
        f"{done_marker}"
    )


async def _assert_reports_progress(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    """Given a turn parked past the status interval, when the bot works on it,
    then a "still working" heartbeat lands once, on schedule, mid-turn."""
    # Arrange: a unique marker so the turn's end is unmistakable, even if a
    # stray message from an earlier turn is still in flight.
    done_marker = new_sentinel("HEARTBEAT")

    # Arrange: wait for the engine to go idle so THIS request starts its own
    # turn. A request injected into a still-closing prior turn (e.g. the launch
    # warm-up) never arms the heartbeat.
    await wait_for_engine_idle(sut)

    # Act: park the turn in a single browser wait that outlives the interval,
    # watching for the heartbeat until the bot signs off with the marker.
    obs = await send_and_watch_status(
        client,
        convo,
        _light_sleep_request(done_marker),
        until_final=lambda m: done_marker in (m.raw_text or ""),
        timeout=MAX_STATUS_TURN_S,
    )
    log.info(
        "status heartbeat: first_ping=%ss completed=%s msgs=%d",
        obs.first_ping_s,
        obs.completed,
        len(obs.chunks),
    )

    # Assert: the turn finished, so it can't bleed into the next test.
    assert obs.completed, (
        f"the parked turn never sent its done-marker within "
        f"{MAX_STATUS_TURN_S:.0f}s; got {len(obs.chunks)} message(s): {obs.chunks}"
    )
    # Assert: a heartbeat was sent while the turn ran.
    assert obs.first_ping_s is not None, (
        f"no '⏳ Still working' heartbeat during a turn that ran past the "
        f"{STATUS_SUT_INTERVAL_S:.0f}s status interval; messages: {obs.chunks}"
    )
    # Assert: it fired at the interval — not immediately (interval-gated)...
    assert obs.first_ping_s >= STATUS_SUT_INTERVAL_S - _PING_FLOOR_SLACK_S, (
        f"heartbeat fired after only {obs.first_ping_s:.1f}s, sooner than the "
        f"{STATUS_SUT_INTERVAL_S:.0f}s interval — it isn't interval-gated"
    )
    # ...and promptly once due.
    assert_within(obs.first_ping_s, MAX_STATUS_PING_S, "status heartbeat")
    # Assert: the heartbeat threads under the request that kicked the turn,
    # rather than floating free in the chat.
    assert obs.first_ping_replies_to_request, (
        "the '⏳ Still working' heartbeat must reply to the request that kicked "
        "the turn, but it was sent as a standalone message"
    )


@pytest.mark.slow
async def test_bot_reports_progress_on_long_turn_dm(
    status_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """Bot pings progress on a turn that outlives the status interval.

    given  a turn parked in one browser wait that runs past STATUS_SUT_INTERVAL_S
    when   the tester asks in a DM
    then   a "still working" heartbeat lands once, at the interval and within
           MAX_STATUS_PING_S, and the turn ends with its done-marker.
    """
    await _assert_reports_progress(status_sut, tester_client, dm)


@pytest.mark.slow
async def test_bot_reports_progress_on_long_turn_group(
    status_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """Bot pings progress on a turn that outlives the status interval.

    given  a turn parked in one browser wait that runs past STATUS_SUT_INTERVAL_S
    when   the tester asks in a group
    then   a "still working" heartbeat lands once, at the interval and within
           MAX_STATUS_PING_S, and the turn ends with its done-marker.
    """
    await _assert_reports_progress(status_sut, tester_client, group)
