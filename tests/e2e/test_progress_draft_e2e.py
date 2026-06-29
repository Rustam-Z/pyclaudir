"""E2E: turning on the live progress draft doesn't break normal DM replies.

With ``HAMROH_PROGRESS_DRAFT_ENABLED`` on, every DM turn streams a live
"working…" draft (Telegram's ``sendMessageDraft``) on the hot path — fired the
instant a message arrives and refreshed every ~5s until the reply lands. This
test guards the regression that matters most: those extra draft calls must not
break or slow the actual answer. It runs on a dedicated ``draft_sut`` (the
toggle is read at startup) so only this file sees draft mode.

Scope: DM only. ``sendMessageDraft`` is private-chat only by Telegram's
contract, so there is no group behavior to test — groups keep the typing
indicator (proved unit-side in ``tests/unit/test_progress_draft.py``). The
ephemeral draft itself surfaces as a transient ``SendMessageTextDraftAction``
typing action; the unit tests pin that send path, so here we assert the durable,
non-flaky outcome — the reply still arrives correctly and promptly.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.config import MAX_TEXT_REPLY_S
from tests.e2e.support.data import recall_prompt
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation

log = logging.getLogger(__name__)


async def test_progress_draft_does_not_break_reply_dm(
    draft_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """A normal DM turn still answers correctly with the progress draft on.

    given  a bot started with the live progress draft enabled
    when   the tester asks a natural question carrying a unique token in a DM
    then   the bot returns the token in a single reply within MAX_TEXT_REPLY_S —
           the per-turn draft streaming hasn't broken or slowed the answer.
    """
    # Arrange: warm-up turn pays the one-time startup cost; not measured.
    await send_and_wait(tester_client, dm, "Hello, are you there?")
    question, token = recall_prompt()

    # Act: ask the timed question on the draft-enabled bot.
    reply = await send_and_wait(tester_client, dm, question)
    log.info(
        "draft-mode reply latency: first=%.2fs complete=%.2fs",
        reply.t_first_s,
        reply.t_complete_s,
    )

    # Assert: the answer is correct, single-message, and prompt.
    assert reply.chunk_count == 1, (
        f"expected exactly 1 message, got {reply.chunk_count}; reply {reply.text!r}"
    )
    assert token in reply.text, (
        f"bot did not return {token!r} with draft mode on; reply was {reply.text!r}"
    )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "draft-mode reply")
