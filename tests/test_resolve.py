from conftest import CLIENT_IP


async def register(client, link_id="acme-tasks", port=47130):
    resp = await client.post("/v1/links", json={"id": link_id, "port": port})
    assert resp.status_code == 201
    return resp.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_resolve_returns_address(client):
    creds = await register(client)
    resp = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ip"] == CLIENT_IP
    assert body["port"] == 47130
    assert body["last_seen"].endswith("Z")


async def test_resolve_without_token_is_401(client):
    await register(client)
    resp = await client.get("/v1/links/acme-tasks")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_resolve_wrong_token_is_404(client):
    creds = await register(client)
    for tok in ["blr_wrong", creds["owner_token"]]:
        resp = await client.get("/v1/links/acme-tasks", headers=bearer(tok))
        assert resp.status_code == 404


async def test_resolve_stale_link_is_404(client, monkeypatch):
    creds = await register(client)
    base = 5_000_000.0
    monkeypatch.setattr("behelink.clock.now", lambda: base)
    resp = await client.patch(
        "/v1/links/acme-tasks", json={}, headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    # 180s TTL: 180s after last heartbeat still fresh, 181s is stale
    monkeypatch.setattr("behelink.clock.now", lambda: base + 180.0)
    fresh = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert fresh.status_code == 200
    monkeypatch.setattr("behelink.clock.now", lambda: base + 181.0)
    stale = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert stale.status_code == 404
    assert stale.json()["type"] == "not_found"


async def test_stale_link_revives_on_heartbeat(client, monkeypatch):
    creds = await register(client)
    monkeypatch.setattr("behelink.clock.now", lambda: 5_000_000.0)
    resp = await client.patch(
        "/v1/links/acme-tasks", json={}, headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    resp = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert resp.status_code == 200
