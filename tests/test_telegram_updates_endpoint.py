import asyncio

from conftest import make_client


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def enable(client, link_id="acme"):
    creds = (await client.post("/v1/links", json={"id": link_id, "port": 1234})).json()
    enabled = (
        await client.post(
            f"/v1/links/{link_id}:enableTelegramRelay",
            headers=bearer(creds["owner_token"]),
        )
    ).json()
    return creds, enabled["path_token"]


async def test_updates_returns_queued_rows_in_seq_order(client):
    creds, path_token = await enable(client)
    await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})
    await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 2})

    resp = await client.get(
        "/v1/links/acme/telegram/updates?wait=0", headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    updates = resp.json()["updates"]
    assert [u["payload"]["update_id"] for u in updates] == [1, 2]
    assert updates[0]["seq"] < updates[1]["seq"]


async def test_update_carries_secret_header_and_iso_received_at(client):
    creds, path_token = await enable(client)
    await client.post(
        f"/v1/tg/acme/{path_token}",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
    )
    updates = (
        await client.get(
            "/v1/links/acme/telegram/updates?wait=0",
            headers=bearer(creds["owner_token"]),
        )
    ).json()["updates"]
    assert updates[0]["secret_header"] == "s3cret"
    assert updates[0]["received_at"].endswith("Z")


async def test_updates_returns_204_when_nothing_queued(client):
    creds, _ = await enable(client)
    resp = await client.get(
        "/v1/links/acme/telegram/updates?wait=0", headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 204


async def test_updates_requires_owner_token(client):
    other = (await client.post("/v1/links", json={"id": "other", "port": 1})).json()
    creds, _ = await enable(client)

    assert (
        await client.get(
            "/v1/links/acme/telegram/updates?wait=0",
            headers=bearer(other["owner_token"]),
        )
    ).status_code == 404
    # The resolve token is not a consumer credential either — single consumer is
    # the property that makes duplicate consumption structurally impossible.
    assert (
        await client.get(
            "/v1/links/acme/telegram/updates?wait=0",
            headers=bearer(creds["resolve_token"]),
        )
    ).status_code == 404
    assert (
        await client.get("/v1/links/acme/telegram/updates?wait=0")
    ).status_code == 401


async def test_redelivery_before_ack_then_gone_after_ack(client):
    creds, path_token = await enable(client)
    headers = bearer(creds["owner_token"])
    await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})

    first = (
        await client.get("/v1/links/acme/telegram/updates?wait=0", headers=headers)
    ).json()["updates"]
    again = (
        await client.get("/v1/links/acme/telegram/updates?wait=0", headers=headers)
    ).json()["updates"]
    assert [u["seq"] for u in first] == [u["seq"] for u in again]

    acked = await client.post(
        "/v1/links/acme/telegram/ack",
        json={"through_seq": first[-1]["seq"]},
        headers=headers,
    )
    assert acked.status_code == 204
    assert (
        await client.get("/v1/links/acme/telegram/updates?wait=0", headers=headers)
    ).status_code == 204


async def test_ack_leaves_later_updates_alone(client):
    creds, path_token = await enable(client)
    headers = bearer(creds["owner_token"])
    await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})
    await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 2})

    updates = (
        await client.get("/v1/links/acme/telegram/updates?wait=0", headers=headers)
    ).json()["updates"]
    await client.post(
        "/v1/links/acme/telegram/ack",
        json={"through_seq": updates[0]["seq"]},
        headers=headers,
    )
    remaining = (
        await client.get("/v1/links/acme/telegram/updates?wait=0", headers=headers)
    ).json()["updates"]
    assert [u["payload"]["update_id"] for u in remaining] == [2]


async def test_ack_requires_owner_token(client):
    other = (await client.post("/v1/links", json={"id": "other", "port": 1})).json()
    await enable(client)
    resp = await client.post(
        "/v1/links/acme/telegram/ack",
        json={"through_seq": 1},
        headers=bearer(other["owner_token"]),
    )
    assert resp.status_code == 404


async def test_limit_is_clamped_to_max_batch(app, client):
    creds, path_token = await enable(client)
    app.state.settings.telegram_updates_max_batch = 2
    for i in range(5):
        await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": i})

    updates = (
        await client.get(
            "/v1/links/acme/telegram/updates?wait=0&limit=100",
            headers=bearer(creds["owner_token"]),
        )
    ).json()["updates"]
    assert len(updates) == 2


async def test_long_poll_wakes_as_soon_as_an_update_is_committed(app, client):
    creds, path_token = await enable(client)

    async def deliver_soon():
        await asyncio.sleep(0.05)
        async with make_client(app) as sender:
            await sender.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})

    asyncio.ensure_future(deliver_soon())
    resp = await asyncio.wait_for(
        client.get(
            "/v1/links/acme/telegram/updates?wait=5",
            headers=bearer(creds["owner_token"]),
        ),
        timeout=2.0,
    )
    assert resp.status_code == 200
    assert resp.json()["updates"][0]["payload"] == {"update_id": 1}


async def test_wait_is_clamped_to_the_configured_ceiling(app, client):
    creds, _ = await enable(client)
    app.state.settings.telegram_updates_wait_max_seconds = 0.05
    resp = await asyncio.wait_for(
        client.get(
            "/v1/links/acme/telegram/updates?wait=999",
            headers=bearer(creds["owner_token"]),
        ),
        timeout=2.0,
    )
    assert resp.status_code == 204


async def test_status_reports_depth_and_enabled(client):
    creds, path_token = await enable(client)
    headers = bearer(creds["owner_token"])
    await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})

    status = (await client.get("/v1/links/acme/telegram", headers=headers)).json()
    assert status["enabled"] is True
    assert status["depth"] == 1
    assert status["oldest_seq"] is not None
    assert status["last_enqueue_at"].endswith("Z")
    assert status["dropped_total"] == 0


async def test_status_on_a_link_that_never_enabled_the_relay(client):
    creds = (await client.post("/v1/links", json={"id": "acme", "port": 1})).json()
    status = (
        await client.get(
            "/v1/links/acme/telegram", headers=bearer(creds["owner_token"])
        )
    ).json()
    assert status == {
        "enabled": False,
        "depth": 0,
        "oldest_seq": None,
        "last_enqueue_at": None,
        "dropped_total": 0,
    }
