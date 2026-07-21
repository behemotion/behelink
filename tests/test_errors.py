async def test_unknown_route_is_problem_json(client):
    resp = await client.get("/v1/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"] == "not_found"
    assert body["status"] == 404
    assert body["title"]
