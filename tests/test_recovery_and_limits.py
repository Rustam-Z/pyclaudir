"""Step 10 invariants: rate limiter, heartbeat, crash-recovery math."""

from __future__ import annotations

import time

import pytest

from pyclaudir.cc_worker import CcWorker
from pyclaudir.rate_limiter import RateLimitExceeded, RateLimiter
from pyclaudir.tools.base import Heartbeat


def test_rate_limiter_allows_under_cap() -> None:
    rl = RateLimiter(limit=3, window_seconds=60)
    rl.check_and_record(1)
    rl.check_and_record(1)
    rl.check_and_record(1)


def test_rate_limiter_blocks_over_cap() -> None:
    rl = RateLimiter(limit=2, window_seconds=60)
    rl.check_and_record(7)
    rl.check_and_record(7)
    with pytest.raises(RateLimitExceeded):
        rl.check_and_record(7)


def test_rate_limiter_per_chat_independent() -> None:
    rl = RateLimiter(limit=1, window_seconds=60)
    rl.check_and_record(1)
    rl.check_and_record(2)  # different chat — must succeed
    with pytest.raises(RateLimitExceeded):
        rl.check_and_record(1)


def test_rate_limiter_window_slides() -> None:
    rl = RateLimiter(limit=1, window_seconds=0.05)
    rl.check_and_record(99)
    with pytest.raises(RateLimitExceeded):
        rl.check_and_record(99)
    time.sleep(0.06)
    rl.check_and_record(99)  # window expired


def test_heartbeat_advances_on_beat() -> None:
    hb = Heartbeat()
    t0 = hb.last_activity
    time.sleep(0.01)
    hb.beat()
    assert hb.last_activity > t0


def test_crash_backoff_math() -> None:
    """Verify the backoff formula and 10-crashes-in-10-min cap."""
    base = CcWorker.CRASH_BACKOFF_BASE
    cap = CcWorker.CRASH_BACKOFF_CAP
    # attempts 1..10
    for attempt in range(1, 11):
        backoff = min(cap, base * (2 ** (attempt - 1)))
        assert backoff <= cap
        if attempt <= 6:
            assert backoff == base * (2 ** (attempt - 1))
        else:
            assert backoff == cap
    assert CcWorker.CRASH_LIMIT == 10
    assert CcWorker.CRASH_WINDOW_SECONDS == 600.0
