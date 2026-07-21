async def register(client, link_id="acme-tasks", port=47130):
    resp = await client.post("/v1/links", json={"id": link_id, "port": port})
    assert resp.status_code == 201
    return resp.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_delete_deregisters(client):
    creds = await register(client)
    resp = await client.delete("/v1/links/acme-tasks", headers=bearer(creds["owner_token"]))
    assert resp.status_code == 204
    assert resp.content == b""
    resolve = await client.get(
        "/v1/links/acme-tasks", headers=bearer(creds["resolve_token"])
    )
    assert resolve.status_code == 404


async def test_delete_frees_the_id(client):
    creds = await register(client)
    await client.delete("/v1/links/acme-tasks", headers=bearer(creds["owner_token"]))
    resp = await client.post("/v1/links", json={"id": "acme-tasks", "port": 1234})
    assert resp.status_code == 201


async def test_delete_wrong_token_is_404(client):
    creds = await register(client)
    resp = await client.delete(
        "/v1/links/acme-tasks", headers=bearer(creds["resolve_token"])
    )
    assert resp.status_code == 404


async def test_rotate_resolve_token(client):
    creds = await register(client)
    resp = await client.post(
        "/v1/links/acme-tasks:rotateResolveToken", headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    new_token = resp.json()["resolve_token"]
    assert new_token.startswith("blr_")
    assert new_token != creds["resolve_token"]
    old = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert old.status_code == 404
    new = await client.get("/v1/links/acme-tasks", headers=bearer(new_token))
    assert new.status_code == 200


async def test_rotate_requires_owner_token(client):
    creds = await register(client)
    resp = await client.post(
        "/v1/links/acme-tasks:rotateResolveToken", headers=bearer(creds["resolve_token"])
    )
    assert resp.status_code == 404
