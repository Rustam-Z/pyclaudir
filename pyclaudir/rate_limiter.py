"""Per-chat outbound rate limiter — fixed-bucket, DB-backed.

State is persisted to the ``rate_limits`` table so counters survive
restarts. A 60-second fixed-minute bucket is used: within each bucket
the count is compared to ``limit``; once exceeded, further calls raise
:class:`RateLimitExceeded` until the bucket rolls over.

The ``notice_sent`` column on each row is a one-shot flag: the *first*
over-limit call in a bucket is told to notify the user (via a path that
bypasses this limiter); subsequent over-limit calls in the same bucket
are silent, so we never spam the chat with throttle notices.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db.database import Database


@dataclass
class RateLimitExceeded(Exception):
    """Raised when a chat has exhausted its budget for the current bucket.

    ``notify`` is True only the first time the limit is crossed in a given
    bucket — the tool layer uses this to send a single throttle notice.
    ``retry_after_s`` is how many seconds remain until the bucket rolls over.
    """

    chat_id: int
    limit: int
    retry_after_s: int
    notify: bool

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"chat {self.chat_id}: more than {self.limit} messages in the current "
            f"{self.retry_after_s}s window"
        )


class RateLimiter:
    def __init__(
        self,
        db: Database,
        limit: int = 20,
        window_seconds: int = 60,
    ) -> None:
        self.db = db
        self.limit = limit
        self.window = window_seconds

    def _bucket(self, now: float | None = None) -> int:
        t = int(now if now is not None else time.time())
        return (t // self.window) * self.window

    async def check_and_record(self, chat_id: int) -> None:
        """Increment the counter for ``chat_id`` in the current bucket.

        If the post-increment count is above ``self.limit`` the increment is
        logically rolled back (count capped at ``limit``) and
        :class:`RateLimitExceeded` is raised. The exception's ``notify``
        field is True only for the very first over-limit call in this bucket
        for this chat.
        """
        now = int(time.time())
        bucket = self._bucket(now)

        await self.db.execute(
            """
            INSERT INTO rate_limits(chat_id, bucket_start, count, notice_sent)
            VALUES (?, ?, 1, 0)
            ON CONFLICT(chat_id, bucket_start) DO UPDATE SET count = count + 1
            """,
            (chat_id, bucket),
        )

        row = await self.db.fetch_one(
            "SELECT count, notice_sent FROM rate_limits WHERE chat_id=? AND bucket_start=?",
            (chat_id, bucket),
        )
        if row is None:  # pragma: no cover - should be impossible
            return
        count = int(row["count"])
        notice_sent = int(row["notice_sent"])

        if count > self.limit:
            # Cap the counter so repeated throws don't inflate it unboundedly.
            await self.db.execute(
                "UPDATE rate_limits SET count=? WHERE chat_id=? AND bucket_start=?",
                (self.limit, chat_id, bucket),
            )
            notify = notice_sent == 0
            if notify:
                await self.db.execute(
                    "UPDATE rate_limits SET notice_sent=1 WHERE chat_id=? AND bucket_start=?",
                    (chat_id, bucket),
                )
            retry_after = max(1, bucket + self.window - now)

            # Opportunistic cleanup of old buckets; cheap, runs only on exceed.
            await self.db.execute(
                "DELETE FROM rate_limits WHERE bucket_start < ?",
                (bucket - 2 * self.window,),
            )

            raise RateLimitExceeded(
                chat_id=chat_id,
                limit=self.limit,
                retry_after_s=retry_after,
                notify=notify,
            )
