"""E2E: a burst of messages is fully handled — DM and group.

given  three distinct tokens
when    the tester fires all three, one per second
then    the bot's replies echo every one (none dropped).

Mirrors "send multiple messages and get responses". With zero debounce the bot
processes them across one or more turns; either way all three must be answered.
"""

from __future__ import annotations

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.helpers import (
    MAX_BURST_S,
    Conversation,
    assert_within,
    measured,
    recall_prompt,
    send_burst,
)


async def _assert_burst_fully_handled(
    client: TelegramClient, convo: Conversation
) -> None:
    # given three natural questions, each carrying a distinct token
    prompts = [recall_prompt() for _ in range(3)]
    texts = [question for question, _ in prompts]
    tokens = [token for _, token in prompts]

    # when all three are fired, one per second
    replies, elapsed = await measured(send_burst(client, convo, texts, tokens))

    # then every token came back (nothing dropped) ...
    for token in tokens:
        assert token in replies, (
            f"burst dropped {token!r}; collected replies were {replies!r}"
        )
    # ... and the whole burst was answered within the limit
    assert_within(elapsed, MAX_BURST_S, "burst")


async def test_handles_message_burst_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_burst_fully_handled(tester_client, dm)


async def test_handles_message_burst_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_burst_fully_handled(tester_client, group)
