"""E2E: an unauthorized group is silently ignored (and logged).

Authorized DM and group are exercised by every other passing test; this proves
the access gate denies a non-allowed group. (Unauthorized DM can't be tested
from the owner account, which is always authorized in a DM.)
"""

from __future__ import annotations

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from hamroh.access import AccessConfig, load_access
from tests.e2e.support.client import expect_silence
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut, set_access
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import unauthorized_rows


@pytest.mark.smoke
async def test_unauthorized_is_silent_and_logged_group(
    hamroh_sut: Sut,
    tester_client: TelegramClient,
    group: Conversation,
    group_id: int,
) -> None:
    """An unauthorized group is silently ignored and logged.

    given  the test group removed from the allowlist (hot-reloaded)
    when   the tester sends a message in the group
    then   the bot stays silent and records an unauthorized_messages row (refusal_sent=0).
    """
    token = new_sentinel("NOAUTH")
    # Drop just this group from the real allowlist; restore the snapshot after so
    # any other groups (and the round-robin pool) stay intact.
    original = load_access(hamroh_sut.access_path)
    deny = AccessConfig(
        "allowlist",
        allowed_users=original.allowed_users,
        allowed_chats=[c for c in original.allowed_chats if c != group_id],
    )
    try:
        # given the test group is no longer in the allowlist
        set_access(hamroh_sut, deny)

        # when the tester sends a message in the group
        replies = await expect_silence(tester_client, group, f"hello {token}", within=8)

        # then the bot stayed silent ...
        assert not replies, (
            f"expected silence from unauthorized group; "
            f"got {[m.raw_text for m in replies]!r}"
        )
        # ... and recorded the denial (groups get refusal_sent=0)
        rows = unauthorized_rows(hamroh_sut.db_path, token)
        assert rows, f"no unauthorized_messages row for {token!r}"
        assert rows[0]["refusal_sent"] == 0, (
            f"group denial must be silent (refusal_sent=0); row={dict(rows[0])}"
        )
    finally:
        # restore so later tests can use the group again
        set_access(hamroh_sut, original)
