"""E2E: the bot answers a direct message correctly and promptly.

given  a warm bot and a natural question carrying a unique token
when    the tester DMs it
then    the reply returns the token within MAX_TEXT_REPLY_S.

A warm-up turn pays the one-time startup cost off the clock, then the timed
turn must both answer correctly and land its first chunk inside the text-reply
limit. (Aggregate p50/p95 across many samples still lives in test_eval_e2e.py.)
"""

from __future__ import annotations

import logging

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.harness import Sut
from tests.e2e.support.helpers import (
    MAX_TEXT_REPLY_S,
    Conversation,
    assert_reply_within,
    recall_prompt,
    send_and_wait,
)

log = logging.getLogger(__name__)


async def test_reply_is_prompt_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    # given a warm bot (the first turn pays startup cost; not measured)
    await send_and_wait(tester_client, dm, "Hello, are you there?")

    # when we DM a natural question and time the reply
    question, token = recall_prompt()
    reply = await send_and_wait(tester_client, dm, question)
    log.info(
        "DM reply latency: first=%.2fs complete=%.2fs",
        reply.t_first_s,
        reply.t_complete_s,
    )

    # then it answered correctly, and promptly
    assert token in reply.text, (
        f"bot did not return {token!r}; reply was {reply.text!r}"
    )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "DM")
