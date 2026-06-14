"""E2E: the bot writes to memory, reads it back, and survives a reset.

write+read (DM and group, separate tests): remember a codeword, confirm it
    lands in a ``data/memories/`` file, and the bot recalls it.
reset (DM only): the codeword survives ``/reset_session`` — proving
    cross-session persistence, not just in-context recall. The reset is an
    owner command, kept in a DM to avoid group command-addressing quirks.
"""

from __future__ import annotations

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.harness import Sut, memory_files_containing
from tests.e2e.support.helpers import (
    MAX_MEMORY_REPLY_S,
    Conversation,
    assert_reply_within,
    new_sentinel,
    send_and_wait,
    wait_until,
)

_REMEMBER = (
    "Remember this codeword and write it to a memory file: {cw}. "
    "Reply with only the word OK."
)
_RECALL = "What was the codeword I asked you to remember? Reply with ONLY the codeword."
# After a reset the shared bot's memory file holds several codewords from
# earlier tests, so recall the full list rather than an ambiguous "the" one.
_RECALL_ALL = "List every codeword you have saved in your memory."


async def _assert_write_and_read(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    # given a unique codeword the bot must persist to memory
    codeword = new_sentinel("BANANA")
    # when we ask it to remember and write the codeword to a memory file
    saved = await send_and_wait(client, convo, _REMEMBER.format(cw=codeword))
    assert_reply_within(saved, MAX_MEMORY_REPLY_S, "memory write")
    # then the codeword is durably on disk ...
    on_disk = await wait_until(
        lambda: memory_files_containing(sut.memories_dir, codeword)
    )
    assert on_disk, f"{codeword!r} not in any memory file; reply was {saved.text!r}"
    # ... and the bot recalls it
    recalled = await send_and_wait(client, convo, _RECALL)
    assert codeword in recalled.text, (
        f"bot did not recall {codeword!r}; reply was {recalled.text!r}"
    )
    assert_reply_within(recalled, MAX_MEMORY_REPLY_S, "memory recall")


async def test_memory_write_and_read_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_write_and_read(pyclaudir_sut, tester_client, dm)


async def test_memory_write_and_read_group(
    pyclaudir_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_write_and_read(pyclaudir_sut, tester_client, group)


async def test_memory_survives_session_reset_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    # given a codeword written to memory
    codeword = new_sentinel("MANGO")
    await send_and_wait(tester_client, dm, _REMEMBER.format(cw=codeword))
    on_disk = await wait_until(
        lambda: memory_files_containing(pyclaudir_sut.memories_dir, codeword)
    )
    assert on_disk, f"{codeword!r} was not persisted to a memory file"

    # when the Claude session is reset (memories survive, context is fresh)
    await send_and_wait(tester_client, dm, "/reset_session", timeout=60)

    # then the bot still recalls it from disk (listed among saved codewords)
    recalled = await send_and_wait(tester_client, dm, _RECALL_ALL)
    assert codeword in recalled.text, (
        f"bot did not recall {codeword!r} after reset; reply was {recalled.text!r}"
    )
    assert_reply_within(recalled, MAX_MEMORY_REPLY_S, "post-reset recall")
