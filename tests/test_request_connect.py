from conftest import make_client


async def register(client, link_id="acme-tasks", port=47130):
    resp = await client.post("/v1/links", json={"id": link_id, "port": port})
    assert resp.status_code == 201
    return resp.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_request_connect_returns_server_candidate(client):
    creds = await register(client)
    resp = await client.post(
        "/v1/links/acme-tasks:requestConnect",
        json={"port": 41000},
        headers=bearer(creds["resolve_token"]),
    )
    assert resp.status_code == 200
    assert resp.json() == {"ip": "203.0.113.10", "port": 47130}


async def test_request_connect_queues_pending_connect(app, client):
    creds = await register(client)
    resp = await client.post(
        "/v1/links/acme-tasks:requestConnect",
        json={"port": 41000},
        headers=bearer(creds["resolve_token"]),
    )
    assert resp.status_code == 200
    record = await app.state.pending_connect_store.wait("acme-tasks", timeout=0.01)
    assert (record.ip, record.port) == ("203.0.113.10", 41000)


async def test_request_connect_without_token_is_401(client):
    await register(client)
    resp = await client.post("/v1/links/acme-tasks:requestConnect", json={"port": 41000})
    assert resp.status_code == 401


async def test_request_connect_wrong_token_is_404(client):
    creds = await register(client)
    resp = await client.post(
        "/v1/links/acme-tasks:requestConnect",
        json={"port": 41000},
        headers=bearer(creds["owner_token"]),
    )
    assert resp.status_code == 404


async def test_request_connect_unknown_link_is_404(client):
    resp = await client.post(
        "/v1/links/ghost:requestConnect", json={"port": 41000}, headers=bearer("blr_x")
    )
    assert resp.status_code == 404


async def test_request_connect_rate_limited_per_token(client):
    creds = await register(client)
    for i in range(10):
        resp = await client.post(
            "/v1/links/acme-tasks:requestConnect",
            json={"port": 41000 + i},
            headers=bearer(creds["resolve_token"]),
        )
        assert resp.status_code == 200, i
    resp = await client.post(
        "/v1/links/acme-tasks:requestConnect",
        json={"port": 42000},
        headers=bearer(creds["resolve_token"]),
    )
    assert resp.status_code == 429
    assert resp.json()["type"] == "rate_limited"
