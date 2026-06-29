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


@dataclass(frozen=True)
class RateLimitConfig:
    """Tuning for :class:`RateLimiter`: per-bucket cap, bucket size, and the
    owner id that is exempt from limiting."""

    limit: int = 20
    window_seconds: int = 60
    owner_id: int | None = None


class RateLimiter:
    def __init__(
        self, db: Database, config: RateLimitConfig = RateLimitConfig()
    ) -> None:
        self.db = db
        self.limit = config.limit
        self.window = config.window_seconds
        self.owner_id = config.owner_id

    def _bucket(self, now: float | None = None) -> int:
        t = int(now if now is not None else time.time())
        return (t // self.window) * self.window

    async def check_and_record(self, user_id: int) -> None:
        """Increment ``user_id``'s counter in the current bucket.

        No-op for the owner. If the post-increment count is above
        ``self.limit``, :class:`RateLimitExceeded` is raised. The exception's
        ``notify`` field is True only for the very first over-limit call in
        this bucket for this user. Increment-and-read and the notice flip are
        each a single atomic statement, so concurrent calls for the same user
        can't slip past the limit or notify twice.
        """
        if self.owner_id is not None and user_id == self.owner_id:
            return

        now = int(time.time())
        bucket = self._bucket(now)

        row = await self.db.execute_returning(
            """
            INSERT INTO rate_limits(user_id, bucket_start, count, notice_sent)
            VALUES (?, ?, 1, 0)
            ON CONFLICT(user_id, bucket_start) DO UPDATE SET count = count + 1
            RETURNING count
            """,
            (user_id, bucket),
        )
        if row is None:  # pragma: no cover - should be impossible
            return
        count = int(row["count"])

        if count > self.limit:
            raise await self._over_limit(user_id, bucket, now)

    async def _over_limit(
        self, user_id: int, bucket: int, now: int
    ) -> RateLimitExceeded:
        """Build the :class:`RateLimitExceeded` for an over-budget user.

        Flips the one-shot notice flag (so only the first over-limit call in
        this bucket notifies) and prunes buckets older than two windows.
        """
        # One-shot notice: only the call that flips the flag notifies.
        flipped = await self.db.execute_returning(
            "UPDATE rate_limits SET notice_sent=1 "
            "WHERE user_id=? AND bucket_start=? AND notice_sent=0 "
            "RETURNING 1",
            (user_id, bucket),
        )
        await self.db.execute(
            "DELETE FROM rate_limits WHERE bucket_start < ?",
            (bucket - 2 * self.window,),
        )
        return RateLimitExceeded(
            user_id=user_id,
            limit=self.limit,
            retry_after_s=max(1, bucket + self.window - now),
            notify=flipped is not None,
        )
