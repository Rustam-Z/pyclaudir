"""Reply-chain expansion: walk our own messages table to enrich context."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pyclaudir.config import Config
from pyclaudir.db.database import Database
from pyclaudir.db.messages import fetch_reply_chain, insert_message
from pyclaudir.engine import format_messages_with_context
from pyclaudir.models import ChatMessage


def _msg(
    mid: int,
    text: str,
    *,
    chat_id: int = -1001234567890,
    user_id: int = 42,
    direction: str = "in",
    reply_to_id: int | None = None,
    reply_to_text: str | None = None,
) -> ChatMessage:
    return ChatMessage(
        chat_id=chat_id,
        message_id=mid,
        user_id=user_id,
        username="alice" if user_id == 42 else "nodira",
        first_name="Alice" if user_id == 42 else "Nodira",
        direction=direction,  # type: ignore[arg-type]
        timestamp=datetime(2026, 4, 11, 10, mid % 60, tzinfo=timezone.utc),
        text=text,
        reply_to_id=reply_to_id,
        reply_to_text=reply_to_text,
    )


@pytest.fixture()
async def db(tmp_path: Path):
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    try:
        yield db
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# fetch_reply_chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_reply_chain_single_hop(db: Database) -> None:
    await insert_message(db, _msg(10, "first thing"))
    await insert_message(db, _msg(11, "reply", reply_to_id=10))
    chain = await fetch_reply_chain(db, chat_id=-1001234567890, reply_to_id=10)
    assert len(chain) == 1
    assert chain[0]["text"] == "first thing"


@pytest.mark.asyncio
async def test_fetch_reply_chain_multi_hop(db: Database) -> None:
    await insert_message(db, _msg(10, "grandparent"))
    await insert_message(db, _msg(11, "parent", reply_to_id=10))
    await insert_message(db, _msg(12, "child", reply_to_id=11))
    chain = await fetch_reply_chain(
        db, chat_id=-1001234567890, reply_to_id=11, max_depth=5
    )
    # Oldest first
    assert [p["text"] for p in chain] == ["grandparent", "parent"]
    assert [p["message_id"] for p in chain] == [10, 11]


@pytest.mark.asyncio
async def test_fetch_reply_chain_respects_depth(db: Database) -> None:
    for i, txt in enumerate(["a", "b", "c", "d", "e"]):
        prev = 10 + i - 1 if i > 0 else None
        await insert_message(db, _msg(10 + i, txt, reply_to_id=prev))
    chain = await fetch_reply_chain(
        db, chat_id=-1001234567890, reply_to_id=14, max_depth=2
    )
    # Walked from 14 up: hops 14, 13. Returned oldest-first: [13, 14].
    assert [p["text"] for p in chain] == ["d", "e"]


@pytest.mark.asyncio
async def test_fetch_reply_chain_stops_at_missing_parent(db: Database) -> None:
    """If the parent isn't in our DB, the walk stops cleanly (returns []).
    Simulates the bot being added to a group where the user replies to a
    pre-existing message we never observed.
    """
    await insert_message(db, _msg(20, "reply to ancient", reply_to_id=999))
    chain = await fetch_reply_chain(db, chat_id=-1001234567890, reply_to_id=999)
    assert chain == []


@pytest.mark.asyncio
async def test_fetch_reply_chain_handles_outbound_parent(db: Database) -> None:
    """User replies to a Nodira message — outbound rows must be reachable too."""
    await insert_message(db, _msg(30, "I think the answer is 42", user_id=99, direction="out"))
    await insert_message(db, _msg(31, "thanks!", reply_to_id=30))
    chain = await fetch_reply_chain(db, chat_id=-1001234567890, reply_to_id=30)
    assert len(chain) == 1
    assert chain[0]["direction"] == "out"
    assert "42" in chain[0]["text"]


@pytest.mark.asyncio
async def test_fetch_reply_chain_chat_scoped(db: Database) -> None:
    """Same message_id in two chats — must not cross-contaminate."""
    await insert_message(db, _msg(50, "in group A", chat_id=-100))
    await insert_message(db, _msg(50, "in group B", chat_id=-200))
    chain_a = await fetch_reply_chain(db, chat_id=-100, reply_to_id=50)
    chain_b = await fetch_reply_chain(db, chat_id=-200, reply_to_id=50)
    assert chain_a[0]["text"] == "in group A"
    assert chain_b[0]["text"] == "in group B"


# ---------------------------------------------------------------------------
# format_messages_with_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_format_with_context_embeds_reply_chain(db: Database) -> None:
    await insert_message(db, _msg(60, "what time is the meeting?"))
    await insert_message(db, _msg(61, "10am", direction="out"))
    new = _msg(62, "thanks!", reply_to_id=61)
    xml = await format_messages_with_context([new], db)
    assert 'reply_to="61"' in xml
    assert "<reply_chain>" in xml
    assert "10am" in xml
    # And the new message body still appears.
    assert "thanks!" in xml


@pytest.mark.asyncio
async def test_format_with_context_walks_multi_hop(db: Database) -> None:
    await insert_message(db, _msg(70, "GP_BODY"))
    await insert_message(db, _msg(71, "P_BODY", reply_to_id=70))
    new = _msg(72, "child", reply_to_id=71)
    xml = await format_messages_with_context([new], db, max_depth=3)
    # Both ancestors present; oldest-first ordering preserved.
    g_idx = xml.find("GP_BODY")
    p_idx = xml.find("P_BODY")
    assert g_idx != -1 and p_idx != -1
    assert g_idx < p_idx, "expected oldest-first ordering inside reply_chain"
    # And the new message body still appears, after the chain.
    assert xml.find("child") > p_idx


@pytest.mark.asyncio
async def test_format_with_context_falls_back_to_inline(db: Database) -> None:
    """Bot is new to the group; user replies to a message we never saw.
    Telegram inlines a snippet — we should use that as a fallback."""
    new = _msg(
        80, "what about that?",
        reply_to_id=999,
        reply_to_text="the original message Nodira never observed",
    )
    xml = await format_messages_with_context([new], db)
    assert 'reply_to="999"' in xml
    assert 'source="telegram_inline"' in xml
    assert "Nodira never observed" in xml


@pytest.mark.asyncio
async def test_format_with_context_no_reply_no_chain(db: Database) -> None:
    plain = _msg(90, "just a normal message")
    xml = await format_messages_with_context([plain], db)
    assert "<reply_chain>" not in xml
    assert "reply_to=" not in xml
    assert "just a normal message" in xml


@pytest.mark.asyncio
async def test_format_with_context_degrades_when_db_none() -> None:
    """No DB → equivalent to the pure formatter; no crash."""
    new = _msg(100, "hi", reply_to_id=42)
    xml = await format_messages_with_context([new], db=None)
    assert "<reply_chain>" not in xml
    assert "hi" in xml
