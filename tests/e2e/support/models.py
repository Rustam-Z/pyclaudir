"""Value objects for the e2e suite: a conversation, a single bot turn, and the
eval scenario dataset.

A ``Scenario`` is one natural request the eval sends N times across DM and group
to measure correctness and latency. Prompts use natural phrasing (never "echo
this token") so the bot's prompt-injection defense stays out of the way.
``check`` decides how a reply counts as a pass:

* ``contains`` — the unique token appears in the reply text
* ``photo``    — the reply includes a photo
* ``any``      — any non-empty reply (used when we only time the path)
"""

from __future__ import annotations

from dataclasses import dataclass


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

    chunks: tuple[str, ...]  # raw text of each Telegram message, in arrival order
    media_kind: str | None  # "photo" | "document" | None
    t_first_s: float  # send -> first reply chunk (seconds)
    t_complete_s: float  # send -> last chunk (seconds)

    @property
    def text(self) -> str:
        """All reply chunks joined with newlines."""
        return "\n".join(self.chunks)

    @property
    def chunk_count(self) -> int:
        """Number of Telegram messages the reply arrived in."""
        return len(self.chunks)


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str  # contains "{token}"
    check: str  # "contains" | "photo" | "any"


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "echo",
        "My reference number is {token}. What is my reference number?",
        "contains",
    ),
    Scenario(
        "memory_write",
        "Remember this note and write it to a memory file: {token}. Reply with OK.",
        "any",
    ),
    Scenario(
        "memory_read",
        "Read your notes memory file and list what is saved there.",
        "any",
    ),
    Scenario(
        "render",
        "Render a tiny HTML table containing {token} and send it to me as a photo.",
        "photo",
    ),
)
