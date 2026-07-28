import asyncio

from behelink.telegram_queue import TelegramQueueWaiter


async def test_wait_returns_immediately_when_notified_first():
    waiter = TelegramQueueWaiter()
    waiter.notify("acme")
    await asyncio.wait_for(waiter.wait("acme", timeout=5.0), timeout=1.0)


async def test_wait_returns_when_notified_while_waiting():
    waiter = TelegramQueueWaiter()

    async def notify_soon():
        await asyncio.sleep(0.01)
        waiter.notify("acme")

    asyncio.ensure_future(notify_soon())
    await asyncio.wait_for(waiter.wait("acme", timeout=5.0), timeout=1.0)


async def test_wait_gives_up_after_timeout():
    waiter = TelegramQueueWaiter()
    await asyncio.wait_for(waiter.wait("acme", timeout=0.01), timeout=1.0)


async def test_notify_for_one_link_does_not_wake_another():
    waiter = TelegramQueueWaiter()
    waiter.notify("other")
    # Should still time out rather than return early on the wrong link's event.
    await asyncio.wait_for(waiter.wait("acme", timeout=0.05), timeout=1.0)


async def test_peek_re_checks_after_clearing_so_a_lost_notify_is_not_a_long_wait():
    # Models the notify that lands between the caller's own DB read and wait()'s
    # clear(): the event is wiped, so only the re-peek can save it from blocking
    # for the full timeout.
    waiter = TelegramQueueWaiter()
    committed = False

    def commit_then_peek():
        nonlocal committed
        committed = True
        return True

    await asyncio.wait_for(
        waiter.wait("acme", timeout=30.0, peek=commit_then_peek), timeout=1.0
    )
    assert committed
