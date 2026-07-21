from behelink.ratelimit import RateLimiter
from conftest import make_client


def test_limiter_allows_up_to_limit(monkeypatch):
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0)
    rl = RateLimiter(limit=3, window_seconds=3600.0)
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("b") is True  # other keys unaffected


def test_limiter_window_expires(monkeypatch):
    rl = RateLimiter(limit=1, window_seconds=3600.0)
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0)
    assert rl.allow("a") is True
    assert rl.allow("a") is False
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0 + 3600.1)
    assert rl.allow("a") is True


async def test_register_rate_limited_per_ip(app):
    async with make_client(app, ip="192.0.2.1") as c:
        for i in range(10):
            resp = await c.post("/v1/links", json={"id": f"link-{i:02d}", "port": 1000 + i})
            assert resp.status_code == 201, i
        resp = await c.post("/v1/links", json={"id": "link-10", "port": 1010})
        assert resp.status_code == 429
        assert resp.json()["type"] == "rate_limited"
        assert resp.headers["retry-after"] == "3600"
    async with make_client(app, ip="192.0.2.2") as other:
        resp = await other.post("/v1/links", json={"id": "link-10", "port": 1010})
        assert resp.status_code == 201
