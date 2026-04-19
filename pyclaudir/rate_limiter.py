"""Per-user inbound rate limiter — fixed-bucket, DB-backed, owner-exempt.

Enforced in :mod:`telegram_io` on inbound DM messages (not groups) before
they reach the engine. Counters persist to the ``rate_limits`` table so
restarts don't reset budgets.

The ``notice_sent`` column on each row is a one-shot flag: the *first*
over-limit message in a given bucket flags ``notify=True`` so the
dispatcher can send a single throttle notice to the user; subsequent
over-limit messages in the same bucket stay silent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db.database import Database


@dataclass
class RateLimitExceeded(Exception):
    """Raised when a user has exhausted their budget for the current bucket.

    ``notify`` is True only the first time the limit is crossed in a given
    bucket for this user — the dispatcher uses this to send a single
    throttle notice per bucket. ``retry_after_s`` is how many seconds
    remain until the bucket rolls over.
    """

    user_id: int
    limit: int
    retry_after_s: int
    notify: bool

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"user {self.user_id}: more than {self.limit} messages in the current "
            f"{self.retry_after_s}s window"
        )


class RateLimiter:
    def __init__(
        self,
        db: Database,
        limit: int = 20,
        window_seconds: int = 60,
        owner_id: int | None = None,
    ) -> None:
        self.db = db
        self.limit = limit
        self.window = window_seconds
        self.owner_id = owner_id

    def _bucket(self, now: float | None = None) -> int:
        t = int(now if now is not None else time.time())
        return (t // self.window) * self.window

    async def check_and_record(self, user_id: int) -> None:
        """Increment ``user_id``'s counter in the current bucket.

        No-op for the owner. If the post-increment count is above
        ``self.limit`` the increment is capped and :class:`RateLimitExceeded`
        is raised. The exception's ``notify`` field is True only for the
        very first over-limit call in this bucket for this user.
        """
        if self.owner_id is not None and user_id == self.owner_id:
            return

        now = int(time.time())
        bucket = self._bucket(now)

        await self.db.execute(
            """
            INSERT INTO rate_limits(user_id, bucket_start, count, notice_sent)
            VALUES (?, ?, 1, 0)
            ON CONFLICT(user_id, bucket_start) DO UPDATE SET count = count + 1
            """,
            (user_id, bucket),
        )

        row = await self.db.fetch_one(
            "SELECT count, notice_sent FROM rate_limits WHERE user_id=? AND bucket_start=?",
            (user_id, bucket),
        )
        if row is None:  # pragma: no cover - should be impossible
            return
        count = int(row["count"])
        notice_sent = int(row["notice_sent"])

        if count > self.limit:
            await self.db.execute(
                "UPDATE rate_limits SET count=? WHERE user_id=? AND bucket_start=?",
                (self.limit, user_id, bucket),
            )
            notify = notice_sent == 0
            if notify:
                await self.db.execute(
                    "UPDATE rate_limits SET notice_sent=1 WHERE user_id=? AND bucket_start=?",
                    (user_id, bucket),
                )
            retry_after = max(1, bucket + self.window - now)

            await self.db.execute(
                "DELETE FROM rate_limits WHERE bucket_start < ?",
                (bucket - 2 * self.window,),
            )

            raise RateLimitExceeded(
                user_id=user_id,
                limit=self.limit,
                retry_after_s=retry_after,
                notify=notify,
            )
