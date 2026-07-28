"""In-process wakeup signalling for the Telegram update long-poll.

Holds no payloads — the queue itself is durable in SQLite (`db.telegram_updates`).
This exists so a consumer blocked in `GET .../telegram/updates` wakes the moment
an update is committed, instead of polling the database on a timer.
"""

import asyncio
from collections.abc import Callable


class TelegramQueueWaiter:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def _event_for(self, link_id: str) -> asyncio.Event:
        return self._events.setdefault(link_id, asyncio.Event())

    def notify(self, link_id: str) -> None:
        self._event_for(link_id).set()

    async def wait(
        self,
        link_id: str,
        timeout: float,
        peek: Callable[[], bool] | None = None,
    ) -> None:
        """Block until an update lands for `link_id`, or `timeout` elapses.

        `peek` should report whether the durable queue already holds rows. It is
        the same clear-then-re-peek structure as `PendingConnectStore.wait()`,
        for the same reason: a `notify()` landing between the caller's own read
        and the `clear()` below would otherwise be erased, stranding a committed
        update until the full timeout expired.
        """
        event = self._event_for(link_id)
        if event.is_set():
            event.clear()
            return
        event.clear()
        # Nothing else can run between the clear() above and `await event.wait()`
        # below except this peek, so a notify racing the clear is caught here.
        if peek is not None and peek():
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return
        event.clear()
