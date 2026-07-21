"""In-memory per-key sliding-window rate limiter (single-instance service)."""

from collections import deque

from behelink import clock


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 3600.0):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = clock.now()
        events = self._events.setdefault(key, deque())
        while events and events[0] <= now - self.window_seconds:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True
