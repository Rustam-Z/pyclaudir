"""E2E: an unauthorized group is silently ignored (and logged).

given  the test group removed from the allowlist (hot-reloaded)
when    the tester sends a message in the group
then    the bot stays silent AND records an unauthorized_messages row.

Authorized DM and group are exercised by every other passing test; this
proves the access gate denies a non-allowed group. (Unauthorized DM can't
be tested from the owner account, which is always authorized in a DM.)
"""

from __future__ import annotations

from telethon import TelegramClient  # type: ignore[import-untyped]

from pyclaudir.access import AccessConfig
from tests.e2e.support.harness import E2EConfig, Sut, set_access, unauthorized_rows
from tests.e2e.support.helpers import Conversation, expect_silence, new_sentinel


async def test_unauthorized_is_silent_and_logged_group(
    pyclaudir_sut: Sut,
    tester_client: TelegramClient,
    group: Conversation,
    e2e_config: E2EConfig,
) -> None:
    token = new_sentinel("NOAUTH")
    owner, gid = e2e_config.owner_id, e2e_config.group_id
    allow = AccessConfig("allowlist", allowed_users=[owner], allowed_chats=[gid])
    deny = AccessConfig("allowlist", allowed_users=[owner], allowed_chats=[])
    try:
        # given the test group is no longer in the allowlist
        set_access(pyclaudir_sut, deny)

        # when the tester sends a message in the group
        replies = await expect_silence(tester_client, group, f"hello {token}", within=8)

        # then the bot stayed silent ...
        assert not replies, (
            f"expected silence from unauthorized group; "
            f"got {[m.raw_text for m in replies]!r}"
        )
        # ... and recorded the denial (groups get refusal_sent=0)
        rows = unauthorized_rows(pyclaudir_sut.db_path, token)
        assert rows, f"no unauthorized_messages row for {token!r}"
        assert rows[0]["refusal_sent"] == 0, (
            f"group denial must be silent (refusal_sent=0); row={dict(rows[0])}"
        )
    finally:
        # restore so later tests can use the group again
        set_access(pyclaudir_sut, allow)
