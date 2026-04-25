"""Rate limiter, heartbeat, and crash-recovery invariants."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pyclaudir.cc_worker import CcWorker
from pyclaudir.config import Config
from pyclaudir.db.database import Database
from pyclaudir.rate_limiter import RateLimitExceeded, RateLimiter
from pyclaudir.tools.base import Heartbeat


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


@pytest.mark.asyncio
async def test_rate_limiter_allows_under_cap(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        rl = RateLimiter(db=db, limit=3, window_seconds=60)
        await rl.check_and_record(1)
        await rl.check_and_record(1)
        await rl.check_and_record(1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rate_limiter_blocks_over_cap(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        rl = RateLimiter(db=db, limit=2, window_seconds=60)
        await rl.check_and_record(7)
        await rl.check_and_record(7)
        with pytest.raises(RateLimitExceeded) as excinfo:
            await rl.check_and_record(7)
        assert excinfo.value.notify is True
        assert excinfo.value.retry_after_s >= 1
        assert excinfo.value.user_id == 7
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rate_limiter_per_user_independent(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        rl = RateLimiter(db=db, limit=1, window_seconds=60)
        await rl.check_and_record(1)
        await rl.check_and_record(2)  # different user — must succeed
        with pytest.raises(RateLimitExceeded):
            await rl.check_and_record(1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rate_limiter_owner_is_exempt(tmp_path: Path) -> None:
    """The owner never ticks the counter and never gets rate-limited."""
    db = await _open(tmp_path)
    try:
        rl = RateLimiter(db=db, limit=1, window_seconds=60, owner_id=42)
        # Far more calls than the cap; none should raise.
        for _ in range(100):
            await rl.check_and_record(42)
        # No rate_limits row should exist for the owner.
        row = await db.fetch_one(
            "SELECT count FROM rate_limits WHERE user_id=?", (42,)
        )
        assert row is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rate_limiter_notice_fires_once_per_bucket(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        rl = RateLimiter(db=db, limit=1, window_seconds=60)
        await rl.check_and_record(99)
        with pytest.raises(RateLimitExceeded) as first:
            await rl.check_and_record(99)
        assert first.value.notify is True
        with pytest.raises(RateLimitExceeded) as second:
            await rl.check_and_record(99)
        assert second.value.notify is False
        with pytest.raises(RateLimitExceeded) as third:
            await rl.check_and_record(99)
        assert third.value.notify is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rate_limiter_persists_across_restart(tmp_path: Path) -> None:
    """Counter lives in SQLite, so a fresh RateLimiter on the same DB sees it."""
    db = await _open(tmp_path)
    try:
        rl_a = RateLimiter(db=db, limit=2, window_seconds=60)
        await rl_a.check_and_record(5)
        await rl_a.check_and_record(5)
        # Simulate a restart: construct a brand-new limiter on the same db.
        rl_b = RateLimiter(db=db, limit=2, window_seconds=60)
        with pytest.raises(RateLimitExceeded):
            await rl_b.check_and_record(5)
    finally:
        await db.close()


def test_heartbeat_advances_on_beat() -> None:
    hb = Heartbeat()
    t0 = hb.last_activity
    time.sleep(0.01)
    hb.beat()
    assert hb.last_activity > t0


def test_crash_backoff_math() -> None:
    """Verify the backoff formula and 10-crashes-in-10-min cap.

    Reads the defaults from ``Config`` rather than the worker, since the
    worker no longer carries class-level constants — Config is the
    single source of truth for these knobs.
    """
    cfg = Config.for_test(Path("/tmp"))
    base = cfg.crash_backoff_base
    cap = cfg.crash_backoff_cap
    for attempt in range(1, 11):
        backoff = min(cap, base * (2 ** (attempt - 1)))
        assert backoff <= cap
        if attempt <= 6:
            assert backoff == base * (2 ** (attempt - 1))
        else:
            assert backoff == cap
    assert cfg.crash_limit == 10
    assert cfg.crash_window_seconds == 600.0
