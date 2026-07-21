import asyncio

from behelink.pending_connect import PendingConnect, PendingConnectStore


async def test_wait_returns_immediately_when_already_queued():
    store = PendingConnectStore(ttl_seconds=10.0)
    store.put("acme-tasks", ip="203.0.113.10", port=41000)
    result = await asyncio.wait_for(store.wait("acme-tasks", timeout=5.0), timeout=1.0)
    assert result == PendingConnect(ip="203.0.113.10", port=41000, expires_at=result.expires_at)


async def test_wait_wakes_up_when_put_arrives_later():
    store = PendingConnectStore(ttl_seconds=10.0)

    async def delayed_put():
        await asyncio.sleep(0.05)
        store.put("acme-tasks", ip="203.0.113.10", port=41000)

    waiter = asyncio.ensure_future(store.wait("acme-tasks", timeout=5.0))
    asyncio.ensure_future(delayed_put())
    result = await asyncio.wait_for(waiter, timeout=1.0)
    assert result is not None
    assert (result.ip, result.port) == ("203.0.113.10", 41000)


async def test_wait_times_out_when_nothing_arrives():
    store = PendingConnectStore(ttl_seconds=10.0)
    result = await asyncio.wait_for(store.wait("acme-tasks", timeout=0.05), timeout=1.0)
    assert result is None


async def test_expired_record_is_not_returned(monkeypatch):
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0)
    store = PendingConnectStore(ttl_seconds=10.0)
    store.put("acme-tasks", ip="203.0.113.10", port=41000)
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0 + 10.1)
    result = await asyncio.wait_for(store.wait("acme-tasks", timeout=0.05), timeout=1.0)
    assert result is None
