"""In-memory pending-connect queue for NAT hole-punch signaling.

Single-instance, disposable by design — a restart losing an in-flight record is fine, the caller
retries. Records live only a few seconds (one hole-punch attempt), unlike the durable `links` table.
"""

import asyncio
from dataclasses import dataclass

from behelink import clock


@dataclass
class PendingConnect:
    ip: str
    port: int
    expires_at: float


class PendingConnectStore:
    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, PendingConnect] = {}
        self._events: dict[str, asyncio.Event] = {}

    def _event_for(self, link_id: str) -> asyncio.Event:
        return self._events.setdefault(link_id, asyncio.Event())

    def _peek(self, link_id: str) -> PendingConnect | None:
        record = self._records.get(link_id)
        if record is None:
            return None
        if clock.now() >= record.expires_at:
            del self._records[link_id]
            return None
        return record

    def put(self, link_id: str, ip: str, port: int) -> None:
        now = clock.now()
        self._records[link_id] = PendingConnect(ip=ip, port=port, expires_at=now + self.ttl_seconds)
        self._event_for(link_id).set()

    async def wait(self, link_id: str, timeout: float) -> PendingConnect | None:
        record = self._peek(link_id)
        if record is not None:
            return record
        event = self._event_for(link_id)
        event.clear()
        # Re-check after clearing: nothing else can run between this line and
        # `await event.wait()` below (no await in between), so if a concurrent
        # `put()` landed between the first peek and this line, it's caught here
        # instead of being lost to the clear().
        record = self._peek(link_id)
        if record is not None:
            return record
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return None
        return self._peek(link_id)
