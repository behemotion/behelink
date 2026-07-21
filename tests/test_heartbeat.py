from conftest import make_client


async def register(client, link_id="acme-tasks", port=47130):
    resp = await client.post("/v1/links", json={"id": link_id, "port": port})
    assert resp.status_code == 201
    return resp.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_heartbeat_refreshes_last_seen_and_ip(app, client, monkeypatch):
    creds = await register(client)
    monkeypatch.setattr("behelink.clock.now", lambda: 5_000_000.0)
    async with make_client(app, ip="198.51.100.99") as roaming:
        resp = await roaming.patch(
            "/v1/links/acme-tasks", json={}, headers=bearer(creds["owner_token"])
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ip"] == "198.51.100.99"
    assert body["port"] == 47130
    assert body["last_seen"] == "1970-02-27T20:53:20Z"


async def test_heartbeat_updates_port(client):
    creds = await register(client)
    resp = await client.patch(
        "/v1/links/acme-tasks", json={"port": 8443}, headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    assert resp.json()["port"] == 8443


async def test_heartbeat_without_token_is_401(client):
    await register(client)
    resp = await client.patch("/v1/links/acme-tasks", json={})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["type"] == "auth"


async def test_heartbeat_wrong_token_is_404(client):
    creds = await register(client)
    for tok in ["blo_definitelywrong", creds["resolve_token"]]:
        resp = await client.patch("/v1/links/acme-tasks", json={}, headers=bearer(tok))
        assert resp.status_code == 404
        assert resp.json()["type"] == "not_found"


async def test_heartbeat_unknown_link_is_404(client):
    resp = await client.patch("/v1/links/ghost", json={}, headers=bearer("blo_x"))
    assert resp.status_code == 404
