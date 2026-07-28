from conftest import make_client

from behelink import db
from behelink.config import Settings


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


def queued(app, link_id="acme"):
    conn = db.connect(app.state.settings.database_path)
    try:
        return db.fetch_updates(conn, link_id, limit=100)
    finally:
        conn.close()


def test_telegram_settings_have_documented_defaults():
    settings = Settings(_env_file=None)
    assert settings.public_base_url == "https://link.behemotion.com"
    assert settings.telegram_relay_enabled is True
    assert settings.telegram_queue_max_depth == 1000
    assert settings.telegram_queue_ttl_seconds == 3600
    assert settings.telegram_updates_wait_max_seconds == 25.0
    assert settings.telegram_updates_max_batch == 100
    assert settings.telegram_webhook_rate_per_minute == 120
    assert settings.telegram_max_payload_bytes == 1048576


async def test_ingress_accepts_and_commits_before_answering_200(app, client):
    _, path_token = await enable(client)
    resp = await client.post(
        f"/v1/tg/acme/{path_token}",
        json={"update_id": 7},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # Read through a *separate* connection: proves the row was committed, not
    # merely staged on the request's connection, before Telegram saw the 200.
    rows = queued(app)
    assert len(rows) == 1
    assert rows[0].secret_header == "s3cret"


async def test_body_is_stored_byte_exact_even_when_behelink_cannot_parse_it(app, client):
    _, path_token = await enable(client)
    body = b'{"anything": "behelink does not parse this",  "spacing": 1}'
    resp = await client.post(
        f"/v1/tg/acme/{path_token}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert queued(app)[0].payload == body


async def test_missing_secret_header_is_stored_as_null(app, client):
    _, path_token = await enable(client)
    await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})
    assert queued(app)[0].secret_header is None


async def test_bad_token_and_disabled_relay_are_byte_identical_404s(client):
    creds, path_token = await enable(client)
    bad_token = await client.post(f"/v1/tg/acme/{path_token}-wrong", json={"update_id": 1})

    await client.post(
        "/v1/links/acme:disableTelegramRelay", headers=bearer(creds["owner_token"])
    )
    disabled = await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})

    assert bad_token.status_code == disabled.status_code == 404
    assert bad_token.text == disabled.text
    assert bad_token.headers["content-type"] == disabled.headers["content-type"]
    assert bad_token.headers["content-type"].startswith("application/problem+json")


async def test_unknown_and_known_links_differ_only_by_the_echoed_id(client):
    # The 404 body must be a pure function of the request, or the ingress becomes
    # a link-ID oracle for an unauthenticated caller.
    await client.post("/v1/links", json={"id": "acme", "port": 1234})
    known = await client.post("/v1/tg/acme/blw_nope", json={"update_id": 1})
    unknown = await client.post("/v1/tg/ghost/blw_nope", json={"update_id": 1})

    assert known.status_code == unknown.status_code == 404
    assert known.text.replace("acme", "ID") == unknown.text.replace("ghost", "ID")


async def test_master_switch_off_makes_the_ingress_404(app, client):
    _, path_token = await enable(client)
    app.state.settings.telegram_relay_enabled = False
    resp = await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})
    assert resp.status_code == 404


async def test_oversized_body_is_rejected_with_413(app, client):
    _, path_token = await enable(client)
    app.state.settings.telegram_max_payload_bytes = 10
    resp = await client.post(
        f"/v1/tg/acme/{path_token}", json={"update_id": 1, "pad": "x" * 100}
    )
    assert resp.status_code == 413
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert queued(app) == []


async def test_full_queue_returns_503_so_telegram_retries(app, client):
    _, path_token = await enable(client)
    app.state.settings.telegram_queue_max_depth = 1
    first = await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})
    second = await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 2})

    assert first.status_code == 200
    # 200 here would be a silent, permanent loss: Telegram only retries non-2xx.
    assert second.status_code == 503
    assert second.headers["Retry-After"] == "10"


async def test_per_link_rate_limit_returns_429(app, client):
    _, path_token = await enable(client)
    app.state.telegram_webhook_limiter.limit = 1
    assert (
        await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})
    ).status_code == 200
    limited = await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 2})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"


async def test_rate_limit_also_applies_per_source_ip(app, client):
    # Two links, one source IP: the per-link buckets are separate, so only a
    # per-IP bucket can stop a single sender from flooding across links.
    _, acme_token = await enable(client, "acme")
    _, other_token = await enable(client, "other")
    app.state.telegram_webhook_limiter.limit = 1

    assert (
        await client.post(f"/v1/tg/acme/{acme_token}", json={"update_id": 1})
    ).status_code == 200
    limited = await client.post(f"/v1/tg/other/{other_token}", json={"update_id": 1})
    assert limited.status_code == 429


async def test_a_different_source_ip_gets_its_own_budget(app, client):
    _, path_token = await enable(client)
    app.state.telegram_webhook_limiter.limit = 1
    assert (
        await client.post(f"/v1/tg/acme/{path_token}", json={"update_id": 1})
    ).status_code == 200

    async with make_client(app, ip="198.51.100.7") as other_ip:
        # Same link, so the per-link bucket is already spent — this must 429 on
        # the link, not silently pass because the IP is fresh.
        resp = await other_ip.post(f"/v1/tg/acme/{path_token}", json={"update_id": 2})
    assert resp.status_code == 429
