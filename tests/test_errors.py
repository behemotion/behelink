async def test_unknown_route_is_problem_json(client):
    resp = await client.get("/v1/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"] == "not_found"
    assert body["status"] == 404
    assert body["title"]


async def test_validation_error_is_problem_json(client):
    resp = await client.post("/v1/links", json={"id": "x", "port": "not-a-port"})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"] == "validation_error"
    assert body["status"] == 422
