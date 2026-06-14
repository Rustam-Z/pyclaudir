"""Drive the bot as a real Telegram user and time its replies.

All Telethon calls here were verified against telethon 1.43.2.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from telethon import TelegramClient, events  # type: ignore[import-untyped]
from telethon.tl.custom.message import Message  # type: ignore[import-untyped]

_QUIET_WINDOW_S = 3.0  # silence that marks a multi-chunk reply as complete
_BURST_TIMEOUT_S = 90.0  # how long to wait for every burst reply to land

# Response-time limits (seconds). Reply limits check the first chunk (t_first_s);
# the others bound how long an observable (reaction, linkage row, full burst,
# fired reminder) takes to appear.
MAX_TEXT_REPLY_S = 5.0  # a plain text answer
MAX_REACTION_S = 5.0  # a turn that adds an emoji reaction
MAX_COMMAND_REPLY_S = 5.0  # an owner control command (/pause, /resume) acks
MAX_USAGE_REPLY_S = 10.0  # /usage shells out to a short-lived `claude --print`
MAX_MEMORY_REPLY_S = 10.0  # a turn that writes/reads a memory file
MAX_SKILL_REPLY_S = 30.0  # a turn that reads a skill first
MAX_REMINDER_REPLY_S = 30.0  # scheduling a reminder (reads the reminder-format skill)
MAX_BURST_S = 10.0  # every reply to a 3-message burst lands
MAX_RENDER_REPLY_S = 60.0  # a turn that renders an image
MAX_REMINDER_FIRE_S = 160.0  # a scheduled reminder actually fires (delayed)

_T = TypeVar("_T")


def new_sentinel(prefix: str) -> str:
    """A unique, exactly-matchable token so each test asserts on its own
    reply (e.g. ``BANANA-1a2b3c4d``)."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def recall_prompt() -> tuple[str, str]:
    """A natural "I told you X, what is X?" prompt and its unique token.

    Preferred over a bare "echo this token" instruction: the bot's system
    prompt rejects blind echo-on-command as a prompt-injection pattern, but
    happily answers a normal question that recalls a value from the same
    message. Returns ``(question, token)``.
    """
    token = new_sentinel("REF")
    return f"My reference number is {token}. What is my reference number?", token


@dataclass(frozen=True)
class Conversation:
    """Where to send and whom to expect the reply from. For a DM both are
    the bot; for a group, send to the group but still expect the bot.

    ``mention`` is the bot's @username, prepended to group messages so the
    bot receives them even with privacy mode on; ``None`` for DMs.
    """

    chat: object
    reply_from: object
    mention: str | None = None


@dataclass(frozen=True)
class Reply:
    """One bot turn as seen by the tester account."""

    text: str  # all reply chunks joined with newlines
    media_kind: str | None  # "photo" | "document" | None
    t_first_s: float  # send -> first reply chunk (seconds)
    t_complete_s: float  # send -> last chunk (seconds)


async def measured(awaitable: Awaitable[_T]) -> tuple[_T, float]:
    """Await ``awaitable`` and return its result paired with the seconds it took.

    Lets a test time any observable (a reaction, a DB row, a fired reminder)
    without each wait helper having to report elapsed time itself.
    """
    start = time.perf_counter()
    result = await awaitable
    return result, time.perf_counter() - start


def assert_within(elapsed_s: float, limit_s: float, what: str) -> None:
    """Fail unless ``elapsed_s`` is under ``limit_s`` seconds."""
    assert elapsed_s < limit_s, (
        f"{what} took {elapsed_s:.2f}s, over the {limit_s:.0f}s limit"
    )


def assert_reply_within(reply: Reply, limit_s: float, what: str) -> None:
    """Fail unless the bot's first reply chunk arrived within ``limit_s`` seconds.

    Latency is judged on ``t_first_s`` (time to the first chunk) — the best
    proxy for felt responsiveness.
    """
    assert_within(reply.t_first_s, limit_s, f"{what} reply's first chunk")


def _media_kind(msg: Message) -> str | None:
    if msg.photo is not None:
        return "photo"
    if msg.document is not None:
        return "document"
    return None


async def _drain_until_quiet(chunks: list[Message], quiet_window: float) -> None:
    """Block until ``quiet_window`` passes with no new chunk appended (the
    bot splits long answers into several Telegram messages)."""
    seen = len(chunks)
    while True:
        await asyncio.sleep(quiet_window)
        if len(chunks) == seen:
            return
        seen = len(chunks)


async def send_and_wait(
    client: TelegramClient, convo: Conversation, text: str, timeout: float = 120.0
) -> Reply:
    """Send ``text`` and collect the bot's (possibly multi-chunk) reply."""
    chunks: list[Message] = []
    first = asyncio.Event()
    last_at = 0.0

    async def _collect(event: events.NewMessage.Event) -> None:
        nonlocal last_at
        chunks.append(event.message)
        last_at = time.perf_counter()
        first.set()

    evt = events.NewMessage(
        chats=convo.chat, from_users=convo.reply_from, incoming=True
    )
    client.add_event_handler(_collect, evt)
    try:
        sent_at = time.perf_counter()
        outgoing = f"@{convo.mention} {text}" if convo.mention else text
        await client.send_message(convo.chat, outgoing)
        await asyncio.wait_for(first.wait(), timeout)
        t_first = time.perf_counter() - sent_at
        await _drain_until_quiet(chunks, _QUIET_WINDOW_S)
    finally:
        client.remove_event_handler(_collect, evt)

    return Reply(
        text="\n".join(m.raw_text or "" for m in chunks),
        media_kind=next((k for m in chunks if (k := _media_kind(m))), None),
        t_first_s=t_first,
        t_complete_s=last_at - sent_at,
    )


async def expect_silence(
    client: TelegramClient, convo: Conversation, text: str, within: float = 8.0
) -> list[Message]:
    """Send ``text`` and collect any bot replies for ``within`` seconds.

    Returns the (hopefully empty) list of replies — used to assert the bot
    stayed silent, e.g. for an unauthorized chat.
    """
    chunks: list[Message] = []

    async def _collect(event: events.NewMessage.Event) -> None:
        chunks.append(event.message)

    evt = events.NewMessage(
        chats=convo.chat, from_users=convo.reply_from, incoming=True
    )
    client.add_event_handler(_collect, evt)
    try:
        outgoing = f"@{convo.mention} {text}" if convo.mention else text
        await client.send_message(convo.chat, outgoing)
        await asyncio.sleep(within)
    finally:
        client.remove_event_handler(_collect, evt)
    return chunks


async def send_burst(
    client: TelegramClient, convo: Conversation, texts: list[str], expect: list[str]
) -> str:
    """Send every text one per second, then collect the bot's replies until each
    substring in ``expect`` has appeared (or a timeout). Returns the joined
    reply text — used to prove a message burst is fully handled."""
    chunks: list[Message] = []

    async def _collect(event: events.NewMessage.Event) -> None:
        chunks.append(event.message)

    evt = events.NewMessage(
        chats=convo.chat, from_users=convo.reply_from, incoming=True
    )
    client.add_event_handler(_collect, evt)
    try:
        for i, text in enumerate(texts):
            if i:
                await asyncio.sleep(1.0)  # pause between messages in a burst
            outgoing = f"@{convo.mention} {text}" if convo.mention else text
            await client.send_message(convo.chat, outgoing)
        deadline = time.monotonic() + _BURST_TIMEOUT_S
        while time.monotonic() < deadline:
            joined = "\n".join(m.raw_text or "" for m in chunks)
            if all(token in joined for token in expect):
                break
            await asyncio.sleep(1.0)
    finally:
        client.remove_event_handler(_collect, evt)
    return "\n".join(m.raw_text or "" for m in chunks)


async def wait_for_message(
    client: TelegramClient, convo: Conversation, token: str, timeout: float = 160.0
) -> str:
    """Wait for a bot message containing ``token`` (e.g. a fired reminder);
    return the joined text seen so far (possibly empty on timeout)."""
    chunks: list[Message] = []
    found = asyncio.Event()

    async def _collect(event: events.NewMessage.Event) -> None:
        chunks.append(event.message)
        if token in (event.message.raw_text or ""):
            found.set()

    evt = events.NewMessage(
        chats=convo.chat, from_users=convo.reply_from, incoming=True
    )
    client.add_event_handler(_collect, evt)
    try:
        try:
            await asyncio.wait_for(found.wait(), timeout)
        except asyncio.TimeoutError:
            pass
    finally:
        client.remove_event_handler(_collect, evt)
    return "\n".join(m.raw_text or "" for m in chunks)


async def send(
    client: TelegramClient,
    convo: Conversation,
    text: str,
    reply_to: int | None = None,
) -> Message:
    """Send ``text`` to the conversation (@mentioning the bot in groups),
    optionally as a reply, and return the sent message — used when a test
    needs the sent message_id."""
    outgoing = f"@{convo.mention} {text}" if convo.mention else text
    return await client.send_message(convo.chat, outgoing, reply_to=reply_to)


async def wait_until(
    predicate: Callable[[], _T], timeout: float = 15.0, interval: float = 1.0
) -> _T:
    """Poll a sync ``predicate`` until it returns a truthy value or timeout.

    Returns that value (or the last falsy one). Used for DB rows that appear
    a beat after a message is delivered.
    """
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        await asyncio.sleep(interval)
        value = predicate()
    return value


def _has_reaction(msg: Message, emoji: str) -> bool:
    reactions = msg.reactions
    if reactions is None:
        return False
    return any(
        getattr(rc.reaction, "emoticon", None) == emoji for rc in reactions.results
    )


async def wait_for_reaction(
    client: TelegramClient, chat: object, message_id: int, emoji: str
) -> bool:
    """Poll a message until the bot's ``emoji`` reaction appears (or timeout).

    Waits slightly past ``MAX_REACTION_S`` with a fine interval so the caller
    can time the true arrival and let ``assert_within`` be the 5s gate, instead
    of the poll cutoff silently masking a reaction that beat the limit.
    """
    deadline = time.monotonic() + MAX_REACTION_S
    while time.monotonic() < deadline:
        msg = await client.get_messages(chat, ids=message_id)
        if msg is not None and _has_reaction(msg, emoji):
            return True
        await asyncio.sleep(0.25)
    return False
