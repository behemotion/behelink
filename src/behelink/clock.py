"""Time source indirection — tests monkeypatch behelink.clock.now."""

import time


def now() -> float:
    return time.time()
