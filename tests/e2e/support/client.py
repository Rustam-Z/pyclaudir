"""Drive the bot as a real Telegram user and time its replies.

Ordered the way a test reads top-to-bottom: address a message, then the
send/act helpers (``send_and_wait`` → ``send`` → ``send_burst``), then the
observe/await helpers (``expect_silence`` → ``wait_for_message`` →
``wait_for_reaction``). Each private helper sits just above its first user.

All Telethon calls here were verified against telethon 1.43.2.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from telethon import TelegramClient, events  # type: ignore[import-untyped]
from telethon.tl.custom.message import Message  # type: ignore[import-untyped]

from tests.e2e.support.config import (
    _BURST_TIMEOUT_S,
    _MULTI_MSG_TIMEOUT_S,
    _QUIET_WINDOW_S,
    MAX_TEXT_REPLY_S,
)
from tests.e2e.support.models import Conversation, Reply, StatusObservation


def _format_outbound(convo: Conversation, text: str) -> str:
    """Address ``text`` to the bot for ``convo``: verbatim in a DM; in a group
    a slash command targets the bot (``/cmd@bot args`` — a bare ``@bot /cmd``
    is not a command at offset 0), other text is @mention-prefixed."""
    if not convo.mention:
        return text
    if text.startswith("/"):
        head, sep, tail = text.partition(" ")
        return f"{head}@{convo.mention}{sep}{tail}"
    return f"@{convo.mention} {text}"


async def _drain_until_quiet(chunks: list[Message], quiet_window: float) -> None:
    """Block until ``quiet_window`` passes with no new chunk appended (the
    bot splits long answers into several Telegram messages)."""
    seen = len(chunks)
    while True:
        await asyncio.sleep(quiet_window)
        if len(chunks) == seen:
            return
        seen = len(chunks)


def _media_kind(msg: Message) -> str | None:
    if msg.photo is not None:
        return "photo"
    if msg.document is not None:
        return "document"
    return None


def _entity_types(chunks: list[Message]) -> frozenset[str]:
    """The Telegram formatting-entity class names across the reply's messages.

    Telegram parses the bot's HTML back into entities (``MessageEntityBold``,
    ``MessageEntityTextUrl``, …); ``raw_text`` drops them, so this is the only
    place a test can see that bold/italic/code/link/quote actually rendered.
    """
    return frozenset(
        type(ent).__name__ for msg in chunks for ent in (msg.entities or ())
    )


async def send(
    client: TelegramClient,
    convo: Conversation,
    text: str,
    reply_to: int | None = None,
) -> Message:
    """Send ``text`` to the conversation (@mentioning the bot in groups),
    optionally as a reply, and return the sent message — used when a test
    needs the sent message_id."""
    outgoing = _format_outbound(convo, text)
    return await client.send_message(convo.chat, outgoing, reply_to=reply_to)


async def send_and_wait(
    client: TelegramClient, convo: Conversation, text: str, timeout: float = 60.0
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
        await client.send_message(convo.chat, _format_outbound(convo, text))
        await asyncio.wait_for(first.wait(), timeout)
        t_first = time.perf_counter() - sent_at
        await _drain_until_quiet(chunks, _QUIET_WINDOW_S)
    finally:
        client.remove_event_handler(_collect, evt)

    return Reply(
        chunks=tuple(m.raw_text or "" for m in chunks),
        media_kind=next((k for m in chunks if (k := _media_kind(m))), None),
        t_first_s=t_first,
        t_complete_s=last_at - sent_at,
        entity_types=_entity_types(chunks),
    )


def has_photo(msg: Message) -> bool:
    """Predicate for :func:`send_and_wait_until` — the message carries a photo."""
    return msg.photo is not None


def is_status_ping(text: str) -> bool:
    """True if ``text`` is the worker's "still working" progress heartbeat."""
    return "Still working" in text


async def send_and_watch_status(
    client: TelegramClient,
    convo: Conversation,
    text: str,
    *,
    until_final: Callable[[Message], bool],
    timeout: float,
) -> StatusObservation:
    """Send ``text``, time the first "still working" heartbeat, and wait for the
    long turn's final answer (``until_final``) to land or ``timeout``.

    The status heartbeat is the bot's promise that a long task is never silent.
    This proves the ping fired on schedule AND that the turn ran to completion,
    so a lingering turn can't bleed into the next test on the shared bot.
    """
    chunks: list[Message] = []
    done = asyncio.Event()
    first_ping_at = 0.0
    first_ping_reply_to: int | None = None
    completed = False

    async def _collect(event: events.NewMessage.Event) -> None:
        nonlocal first_ping_at, first_ping_reply_to, completed
        msg = event.message
        chunks.append(msg)
        if not first_ping_at and is_status_ping(msg.raw_text or ""):
            first_ping_at = time.perf_counter()
            first_ping_reply_to = msg.reply_to_msg_id
        if until_final(msg):
            completed = True
            done.set()

    evt = events.NewMessage(
        chats=convo.chat, from_users=convo.reply_from, incoming=True
    )
    client.add_event_handler(_collect, evt)
    try:
        sent_at = time.perf_counter()
        sent = await client.send_message(convo.chat, _format_outbound(convo, text))
        try:
            await asyncio.wait_for(done.wait(), timeout)
        except asyncio.TimeoutError:
            pass
    finally:
        client.remove_event_handler(_collect, evt)

    return StatusObservation(
        first_ping_s=(first_ping_at - sent_at) if first_ping_at else None,
        completed=completed,
        chunks=tuple(m.raw_text or "" for m in chunks),
        first_ping_replies_to_request=first_ping_reply_to == sent.id,
    )


async def send_and_wait_until(
    client: TelegramClient,
    convo: Conversation,
    text: str,
    *,
    until: Callable[[Message], bool],
    timeout: float,
) -> Reply:
    """Send ``text`` and keep collecting the bot's messages until one satisfies
    ``until`` (e.g. carries a photo), or ``timeout`` elapses.

    A long agent turn emits several progress messages ("on it…", "searching…")
    before the real result lands. The quiet-window helpers stop at the first of
    those; this keeps waiting — through any number of interim messages — for the
    one that actually answers. ``t_complete_s`` is the time to that matching
    message (the felt "time to the answer"); on timeout it's the last message
    seen and ``media_kind`` reveals that no photo arrived.
    """
    chunks: list[Message] = []
    matched = asyncio.Event()
    first_at = last_at = matched_at = 0.0

    async def _collect(event: events.NewMessage.Event) -> None:
        nonlocal first_at, last_at, matched_at
        now = time.perf_counter()
        first_at = first_at or now
        last_at = now
        chunks.append(event.message)
        if not matched.is_set() and until(event.message):
            matched_at = now
            matched.set()

    evt = events.NewMessage(
        chats=convo.chat, from_users=convo.reply_from, incoming=True
    )
    client.add_event_handler(_collect, evt)
    try:
        sent_at = time.perf_counter()
        await client.send_message(convo.chat, _format_outbound(convo, text))
        try:
            await asyncio.wait_for(matched.wait(), timeout)
        except asyncio.TimeoutError:
            pass
    finally:
        client.remove_event_handler(_collect, evt)

    end = matched_at or last_at
    return Reply(
        chunks=tuple(m.raw_text or "" for m in chunks),
        media_kind=next((k for m in chunks if (k := _media_kind(m))), None),
        t_first_s=(first_at - sent_at) if first_at else 0.0,
        t_complete_s=(end - sent_at) if end else 0.0,
    )


async def send_and_wait_for_chunks(
    client: TelegramClient,
    convo: Conversation,
    text: str,
    expected_chunks: int,
) -> Reply:
    """Send ``text`` and collect the reply until ``expected_chunks`` messages
    arrive or ``_MULTI_MSG_TIMEOUT_S`` elapses — used by the split-reply test,
    where gaps between the bot's messages would trip a quiet-window cutoff."""
    chunks: list[Message] = []
    enough = asyncio.Event()
    first_at = last_at = 0.0

    async def _collect(event: events.NewMessage.Event) -> None:
        nonlocal first_at, last_at
        last_at = time.perf_counter()
        first_at = first_at or last_at
        chunks.append(event.message)
        if len(chunks) >= expected_chunks:
            enough.set()

    evt = events.NewMessage(
        chats=convo.chat, from_users=convo.reply_from, incoming=True
    )
    client.add_event_handler(_collect, evt)
    try:
        sent_at = time.perf_counter()
        await client.send_message(convo.chat, _format_outbound(convo, text))
        try:
            await asyncio.wait_for(enough.wait(), _MULTI_MSG_TIMEOUT_S)
        except asyncio.TimeoutError:
            pass
    finally:
        client.remove_event_handler(_collect, evt)
    return Reply(
        chunks=tuple(m.raw_text or "" for m in chunks),
        media_kind=next((k for m in chunks if (k := _media_kind(m))), None),
        t_first_s=first_at - sent_at,
        t_complete_s=last_at - sent_at,
    )


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
            await client.send_message(convo.chat, _format_outbound(convo, text))
        deadline = time.monotonic() + _BURST_TIMEOUT_S
        while time.monotonic() < deadline:
            joined = "\n".join(m.raw_text or "" for m in chunks)
            if all(token in joined for token in expect):
                break
            await asyncio.sleep(1.0)
    finally:
        client.remove_event_handler(_collect, evt)
    return "\n".join(m.raw_text or "" for m in chunks)


async def expect_silence(
    client: TelegramClient, convo: Conversation, text: str, within: float = 15.0
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
        await client.send_message(convo.chat, _format_outbound(convo, text))
        await asyncio.sleep(within)
    finally:
        client.remove_event_handler(_collect, evt)
    return chunks


async def wait_for_message(
    client: TelegramClient, convo: Conversation, token: str, timeout: float = 60.0
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

    Waits slightly past ``MAX_TEXT_REPLY_S`` with a fine interval so the caller
    can time the true arrival and let ``assert_within`` be the 5s gate, instead
    of the poll cutoff silently masking a reaction that beat the limit.
    """
    deadline = time.monotonic() + MAX_TEXT_REPLY_S
    while time.monotonic() < deadline:
        msg = await client.get_messages(chat, ids=message_id)
        if msg is not None and _has_reaction(msg, emoji):
            return True
        await asyncio.sleep(0.25)
    return False
