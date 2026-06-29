"""E2E: the bot writes to memory, reads it back, searches it, and survives a reset.

write+read (DM and group, separate tests): remember a codeword, confirm it
    lands in a ``data/memories/`` file, and the bot recalls it.
search (DM and group, separate tests): seed a fact under an unhelpful filename,
    ask a content question, and confirm the bot answers it AND actually called
    ``memory_search`` to find it (not list + read-everything).
reset (DM only): the codeword survives ``/reset_session`` — proving
    cross-session persistence, not just in-context recall. The reset is an
    owner command, kept in a DM to avoid group command-addressing quirks.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import memory_files_containing, tool_calls_since
from tests.e2e.support.config import MAX_MEMORY_REPLY_S
from tests.e2e.support.waits import wait_until

_REMEMBER = (
    "Remember this codeword and write it to a memory file: {cw}. "
    "Reply with only the word OK."
)
_RECALL = "What was the codeword I asked you to remember? Reply with ONLY the codeword."
# After a reset the shared bot's memory file holds several codewords from
# earlier tests, so recall the full list rather than an ambiguous "the" one.
_RECALL_ALL = "List every codeword you have saved in your memory."
_SEARCH = (
    "Search your memory for {cw} and tell me the launch date saved for it. "
    "Reply with ONLY the date."
)


async def _assert_write_and_read(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    codeword = new_sentinel("BANANA")
    saved = await send_and_wait(client, convo, _REMEMBER.format(cw=codeword))
    assert_reply_within(saved, MAX_MEMORY_REPLY_S, "memory write")
    on_disk = await wait_until(
        lambda: memory_files_containing(sut.memories_dir, codeword)
    )
    assert on_disk, f"{codeword!r} not in any memory file; reply was {saved.text!r}"
    recalled = await send_and_wait(client, convo, _RECALL)
    assert codeword in recalled.text, (
        f"bot did not recall {codeword!r}; reply was {recalled.text!r}"
    )
    assert_reply_within(recalled, MAX_MEMORY_REPLY_S, "memory recall")


async def _assert_search_finds_seeded_fact(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    # Seed a fact under a filename that gives no hint of its contents, so the
    # only way to answer is to search the TEXT — not guess the right file to read.
    codeword = new_sentinel("KIWI")
    launch_date = "2027-03-14"
    seeded = sut.memories_dir / "archive" / "misc-notes.md"
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_text(f"Project {codeword} launch date: {launch_date}\n")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    reply = await send_and_wait(client, convo, _SEARCH.format(cw=codeword))

    assert launch_date in reply.text, (
        f"bot did not recall the seeded date; reply was {reply.text!r}"
    )
    tools = {row["tool_name"] for row in tool_calls_since(sut.db_path, since)}
    assert "memory_search" in tools, (
        f"bot answered without calling memory_search; tools used: {sorted(tools)}"
    )
    assert_reply_within(reply, MAX_MEMORY_REPLY_S, "memory search")


async def test_memory_write_and_read_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """The bot writes a codeword to memory and reads it back in a DM.

    given  a unique codeword
    when   the owner asks the bot to remember it in a DM
    then   it lands in a memory file and the bot recalls it within MAX_MEMORY_REPLY_S.
    """
    await _assert_write_and_read(hamroh_sut, tester_client, dm)


@pytest.mark.smoke
async def test_memory_write_and_read_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot writes a codeword to memory and reads it back in a group.

    given  a unique codeword
    when   the owner asks the bot to remember it in a group
    then   it lands in a memory file and the bot recalls it within MAX_MEMORY_REPLY_S.
    """
    await _assert_write_and_read(hamroh_sut, tester_client, group)


async def test_memory_search_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """The bot searches memory contents to answer a question in a DM.

    given  a fact seeded under a filename that hides its contents
    when   the owner asks a content question about it in a DM
    then   the bot calls memory_search, answers correctly, within MAX_MEMORY_REPLY_S.
    """
    await _assert_search_finds_seeded_fact(hamroh_sut, tester_client, dm)


@pytest.mark.smoke
async def test_memory_search_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot searches memory contents to answer a question in a group.

    given  a fact seeded under a filename that hides its contents
    when   the owner asks a content question about it in a group
    then   the bot calls memory_search, answers correctly, within MAX_MEMORY_REPLY_S.
    """
    await _assert_search_finds_seeded_fact(hamroh_sut, tester_client, group)


@pytest.mark.smoke
async def test_memory_survives_session_reset_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """A codeword in memory survives /reset_session in a DM.

    given  a codeword written to a memory file
    when   the Claude session is reset in a DM
    then   the bot still recalls it from disk within MAX_MEMORY_REPLY_S.
    """
    codeword = new_sentinel("MANGO")
    await send_and_wait(tester_client, dm, _REMEMBER.format(cw=codeword))
    on_disk = await wait_until(
        lambda: memory_files_containing(hamroh_sut.memories_dir, codeword)
    )
    assert on_disk, f"{codeword!r} was not persisted to a memory file"

    # reset drops the in-context session; memories on disk survive
    await send_and_wait(tester_client, dm, "/reset_session", timeout=60)

    recalled = await send_and_wait(tester_client, dm, _RECALL_ALL)
    assert codeword in recalled.text, (
        f"bot did not recall {codeword!r} after reset; reply was {recalled.text!r}"
    )
    assert_reply_within(recalled, MAX_MEMORY_REPLY_S, "post-reset recall")
