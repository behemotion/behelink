async def test_register_returns_tokens_once(client):
    resp = await client.post("/v1/links", json={"id": "acme-tasks", "port": 47130})
    assert resp.status_code == 201
    body = resp.json()
    assert body["owner_token"].startswith("blo_")
    assert body["resolve_token"].startswith("blr_")
    assert set(body) == {"owner_token", "resolve_token"}


async def test_register_duplicate_id_conflicts(client):
    first = await client.post("/v1/links", json={"id": "acme-tasks", "port": 47130})
    assert first.status_code == 201
    resp = await client.post("/v1/links", json={"id": "acme-tasks", "port": 47130})
    assert resp.status_code == 409
    assert resp.json()["type"] == "conflict"


async def test_register_rejects_bad_id(client):
    for bad in ["ab", "-abc", "abc-", "UPPER", "a" * 64, "we:ird"]:
        resp = await client.post("/v1/links", json={"id": bad, "port": 47130})
        assert resp.status_code == 422, bad
        assert resp.json()["type"] == "validation_error"


async def test_register_rejects_bad_port(client):
    for bad in [0, 65536, -1]:
        resp = await client.post("/v1/links", json={"id": "acme-tasks", "port": bad})
        assert resp.status_code == 422, bad
