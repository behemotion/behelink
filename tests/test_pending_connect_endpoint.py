import asyncio

from conftest import make_client


async def register(client, link_id="acme-tasks", port=47130):
    resp = await client.post("/v1/links", json={"id": link_id, "port": port})
    assert resp.status_code == 201
    return resp.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_returns_immediately_when_already_queued(app, client):
    creds = await register(client)
    app.state.pending_connect_store.put("acme-tasks", ip="203.0.113.10", port=41000)
    resp = await client.get(
        "/v1/links/acme-tasks/pending-connect", headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    assert resp.json() == {"ip": "203.0.113.10", "port": 41000}


async def test_wakes_up_when_request_connect_arrives(app, client):
    creds = await register(client)

    async def fire_request_connect():
        await asyncio.sleep(0.05)
        async with make_client(app) as cli:
            await cli.post(
                "/v1/links/acme-tasks:requestConnect",
                json={"port": 41000},
                headers=bearer(creds["resolve_token"]),
            )

    asyncio.ensure_future(fire_request_connect())
    resp = await asyncio.wait_for(
        client.get(
            "/v1/links/acme-tasks/pending-connect?wait=2",
            headers=bearer(creds["owner_token"]),
        ),
        timeout=1.0,
    )
    assert resp.status_code == 200
    assert resp.json()["port"] == 41000


async def test_times_out_with_204_when_nothing_arrives(client):
    creds = await register(client)
    resp = await client.get(
        "/v1/links/acme-tasks/pending-connect?wait=0.05", headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 204


async def test_without_token_is_401(client):
    await register(client)
    resp = await client.get("/v1/links/acme-tasks/pending-connect")
    assert resp.status_code == 401


async def test_wrong_token_is_404(client):
    creds = await register(client)
    resp = await client.get(
        "/v1/links/acme-tasks/pending-connect", headers=bearer(creds["resolve_token"])
    )
    assert resp.status_code == 404


async def test_unknown_link_is_404(client):
    resp = await client.get("/v1/links/ghost/pending-connect", headers=bearer("blo_x"))
    assert resp.status_code == 404


async def test_wait_is_clamped_to_max(app, client, monkeypatch):
    creds = await register(client)
    app.state.settings.pending_connect_wait_max_seconds = 0.05
    resp = await asyncio.wait_for(
        client.get(
            "/v1/links/acme-tasks/pending-connect?wait=999",
            headers=bearer(creds["owner_token"]),
        ),
        timeout=1.0,
    )
    assert resp.status_code == 204
