"""Per-chat outbound rate limiter — sliding window, in-memory.

Step 10 will persist windows to the ``rate_limits`` table; for now we keep
state in process. The interface is stable so the swap is local.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimitExceeded(Exception):
    pass


class RateLimiter:
    def __init__(self, limit: int = 20, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._events: dict[int, deque[float]] = defaultdict(deque)

    def check_and_record(self, chat_id: int) -> None:
        now = time.monotonic()
        events = self._events[chat_id]
        cutoff = now - self.window
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= self.limit:
            raise RateLimitExceeded(
                f"chat {chat_id}: more than {self.limit} messages in {self.window:.0f}s"
            )
        events.append(now)
