"""E2E: the latency + correctness eval runs as part of the suite.

The DM and group cases are separate tests (they fail and run independently).
Latency is reported, not gated: a single sample flakes near an SLO, so the
table is informational (raise ``E2E_EVAL_RUNS`` for trustworthy percentiles).
The correctness pass-rate over the matrix is the stable signal we assert.
A warm-up turn pays the one-time startup cost off the clock. Both tests reuse
the one session bot via ``hamroh_sut`` — no second bot is spawned.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.client import send_and_wait
from tests.e2e.support.eval import chat_label, run_eval
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation

log = logging.getLogger(__name__)
_RUNS = int(os.environ.get("E2E_EVAL_RUNS", "1"))
_MIN_PASS = float(os.environ.get("E2E_EVAL_MIN_PASS", "0.9"))


async def _eval_chat(
    client: TelegramClient, convo: Conversation, db_path: Path
) -> None:
    report = await run_eval(client, convo, db_path, _RUNS)
    chat = chat_label(convo)
    log.info("\n=== eval %s (%d runs/scenario) ===\n%s", chat, _RUNS, report.table)
    # latency is only logged; the pass-rate is the gated signal
    assert report.pass_rate >= _MIN_PASS, (
        f"{chat} eval pass-rate {report.pass_rate:.0%} < {_MIN_PASS:.0%}\n{report.table}"
    )


@pytest.mark.smoke
@pytest.mark.slow
async def test_eval_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """Eval matrix passes for the DM chat.

    given  a warm bot and the shared scenario set
    when   each scenario runs E2E_EVAL_RUNS times in a DM
    then   the latency table is logged and the pass-rate stays >= E2E_EVAL_MIN_PASS.
    """
    # warm-up turn pays the one-time startup cost off the clock
    await send_and_wait(tester_client, dm, "Hello, are you there?")
    await _eval_chat(tester_client, dm, hamroh_sut.db_path)


@pytest.mark.smoke
@pytest.mark.slow
async def test_eval_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """Eval matrix passes for the group chat.

    given  a warm bot and the shared scenario set
    when   each scenario runs E2E_EVAL_RUNS times in a group
    then   the latency table is logged and the pass-rate stays >= E2E_EVAL_MIN_PASS.
    """
    await _eval_chat(tester_client, group, hamroh_sut.db_path)
