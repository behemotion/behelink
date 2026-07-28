import pytest

from behelink import db, tokens


async def register(client, link_id="acme", port=1234):
    resp = await client.post("/v1/links", json={"id": link_id, "port": port})
    assert resp.status_code == 201
    return resp.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_enable_returns_webhook_url_containing_the_path_token(client):
    creds = await register(client)
    resp = await client.post(
        "/v1/links/acme:enableTelegramRelay", headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["path_token"].startswith("blw_")
    assert body["webhook_url"] == (
        f"https://link.behemotion.com/v1/tg/acme/{body['path_token']}"
    )


async def test_enable_is_idempotent_and_keeps_the_same_token(client):
    creds = await register(client)
    headers = bearer(creds["owner_token"])
    first = (await client.post("/v1/links/acme:enableTelegramRelay", headers=headers)).json()
    second = (await client.post("/v1/links/acme:enableTelegramRelay", headers=headers)).json()
    assert first["path_token"] == second["path_token"]


async def test_rotate_invalidates_the_old_token(client):
    creds = await register(client)
    headers = bearer(creds["owner_token"])
    old = (await client.post("/v1/links/acme:enableTelegramRelay", headers=headers)).json()
    new = (await client.post("/v1/links/acme:rotateWebhookPathToken", headers=headers)).json()
    assert new["path_token"] != old["path_token"]

    stale = await client.post(f"/v1/tg/acme/{old['path_token']}", json={"update_id": 1})
    assert stale.status_code == 404
    fresh = await client.post(f"/v1/tg/acme/{new['path_token']}", json={"update_id": 1})
    assert fresh.status_code == 200


async def test_enable_requires_owner_token_not_resolve_token(client):
    creds = await register(client)
    resp = await client.post(
        "/v1/links/acme:enableTelegramRelay", headers=bearer(creds["resolve_token"])
    )
    assert resp.status_code == 404


async def test_enable_without_a_bearer_token_is_401(client):
    await register(client)
    resp = await client.post("/v1/links/acme:enableTelegramRelay")
    assert resp.status_code == 401


async def test_enable_on_unknown_link_is_404(client):
    resp = await client.post(
        "/v1/links/ghost:enableTelegramRelay", headers=bearer("blo_x")
    )
    assert resp.status_code == 404


async def test_enable_is_404_when_the_master_switch_is_off(app, client):
    creds = await register(client)
    app.state.settings.telegram_relay_enabled = False
    resp = await client.post(
        "/v1/links/acme:enableTelegramRelay", headers=bearer(creds["owner_token"])
    )
    # Handing out a webhook_url the ingress would refuse is worse than refusing here.
    assert resp.status_code == 404


@pytest.mark.xfail(reason="status endpoint lands in Task 7", strict=True)
async def test_disable_purges_queued_updates(client):
    creds = await register(client)
    headers = bearer(creds["owner_token"])
    enabled = (await client.post("/v1/links/acme:enableTelegramRelay", headers=headers)).json()
    await client.post(f"/v1/tg/acme/{enabled['path_token']}", json={"update_id": 1})

    resp = await client.post("/v1/links/acme:disableTelegramRelay", headers=headers)
    assert resp.status_code == 204

    status = (await client.get("/v1/links/acme/telegram", headers=headers)).json()
    assert status["enabled"] is False
    assert status["depth"] == 0


async def test_path_token_is_not_echoed_by_the_status_endpoint(client):
    creds = await register(client)
    headers = bearer(creds["owner_token"])
    enabled = (await client.post("/v1/links/acme:enableTelegramRelay", headers=headers)).json()

    status = await client.get("/v1/links/acme/telegram", headers=headers)
    assert enabled["path_token"] not in status.text


async def test_path_token_is_stored_hashed_not_in_cleartext(app, client):
    creds = await register(client)
    enabled = (
        await client.post(
            "/v1/links/acme:enableTelegramRelay", headers=bearer(creds["owner_token"])
        )
    ).json()

    conn = db.connect(app.state.settings.database_path)
    link = db.get_link(conn, "acme")
    assert link.telegram_path_token_hash == tokens.hash_token(enabled["path_token"])
    assert link.telegram_path_token_hash != enabled["path_token"]
