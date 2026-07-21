# behelink NAT Hole-Punch Signaling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a UDP self-STUN reflector, `POST /v1/links/{id}:requestConnect`, and `GET
/v1/links/{id}/pending-connect` to behelink, additive to the shipped `/v1/links` CRUD, per
`docs/superpowers/specs/2026-07-21-behelink-holepunch-signaling-design.md`.

**Architecture:** A second `asyncio.DatagramProtocol` listener runs on the same event loop as the
existing FastAPI/uvicorn process (started via FastAPI's lifespan, not a separate service). It
answers a bearer-token-gated UDP probe with the sender's observed `(ip, port)`. Two new HTTP
endpoints let a CLI hand its self-STUN'd candidate to behelink and let a server long-poll for it,
using an in-memory, `asyncio.Event`-backed store — no new SQLite table, no `links` schema change.

**Tech Stack:** Python ≥3.12, FastAPI, uvicorn, stdlib `asyncio`/`json`/`sqlite3`/`dataclasses` —
no new dependencies.

## Global Constraints

- Python ≥ 3.12; no new third-party dependencies (everything needed is stdlib or already in
  `pyproject.toml`).
- `BEHELINK_` env prefix on every new setting (`config.py`'s `SettingsConfigDict(env_prefix="BEHELINK_")`).
- RFC 9457 `application/problem+json` errors on every new HTTP error path, via the existing
  `ProblemError`/`install_handlers` machinery in `errors.py` — no new error-handling code.
- Bearer auth, constant-time token compare (`tokens.verify_token`), 404-for-wrong-or-missing-token
  on link-scoped resources — same posture as every existing endpoint.
- `db.py`'s `links` table schema is unchanged: no new columns, no new tables. Pending-connect state
  is in-memory only.
- Tests follow the existing `httpx.AsyncClient`-against-test-app style (`tests/conftest.py`); run
  via `uv run pytest` from the `behelink/` repo root.
- The UDP reflector is tested by calling `ReflectorProtocol` methods directly (no real socket bind
  in tests) — real datagram I/O only happens in production, via FastAPI's lifespan.

---

## File Structure

- **Modify `src/behelink/config.py`** — six new `BEHELINK_`-prefixed settings (reflector port/host,
  reflector and requestConnect rate limits, pending-connect TTL and wait-clamp, reflector max
  payload size).
- **Modify `src/behelink/db.py`** — one new read function, `find_link_by_token_hash`, used only by
  the reflector. No schema change.
- **Create `src/behelink/pending_connect.py`** — `PendingConnectStore`: in-memory, TTL'd, keyed by
  `link_id`, `asyncio.Event`-driven wake-up for the long-poll. Single responsibility: hole-punch
  signaling state, nothing else.
- **Create `src/behelink/reflector.py`** — `ReflectorProtocol(asyncio.DatagramProtocol)`: the UDP
  self-STUN echo. Single responsibility: validate a token, echo the sender's observed address.
- **Modify `src/behelink/main.py`** — add the lifespan that binds the UDP listener, construct the
  new rate limiters and the pending-connect store on `app.state`, add the two new route handlers.
- **Modify `README.md`** — extend the API and Configuration tables.
- **Modify `deploy.md`** — document the new UDP port and the `ufw` rule it needs (documentation
  only — applying it to the live box is a separate, manual operator step, called out explicitly).
- **Modify `HARNESS-DIVERGENCES.md`** — new entry for the directly-`ufw`-exposed UDP port.
- **Modify (umbrella repo) `../docs/CONVENTIONS.md` and `../docs/HARNESS-PLAN.md`** — register the
  new port. This is a **different git repository** (the BEHEMOTION umbrella) — its own commit, not
  bundled with behelink's commits.
- **Create test files:** `tests/test_pending_connect_store.py`, `tests/test_reflector.py`,
  `tests/test_request_connect.py`, `tests/test_pending_connect_endpoint.py`. **Modify**
  `tests/test_db.py` (add `find_link_by_token_hash` cases).

---

### Task 1: `db.find_link_by_token_hash`

**Files:**
- Modify: `src/behelink/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `find_link_by_token_hash(conn: sqlite3.Connection, token_hash: str) -> Link | None` —
  matches a link whose `owner_token_hash` **or** `resolve_token_hash` equals `token_hash`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
def test_find_link_by_token_hash_matches_owner(conn):
    db.insert_link(conn, make_link())
    got = db.find_link_by_token_hash(conn, "o" * 64)
    assert got == make_link()


def test_find_link_by_token_hash_matches_resolve(conn):
    db.insert_link(conn, make_link())
    got = db.find_link_by_token_hash(conn, "r" * 64)
    assert got == make_link()


def test_find_link_by_token_hash_no_match_returns_none(conn):
    db.insert_link(conn, make_link())
    assert db.find_link_by_token_hash(conn, "x" * 64) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_db.py -k find_link_by_token_hash -v`
Expected: FAIL with `AttributeError: module 'behelink.db' has no attribute 'find_link_by_token_hash'`

- [ ] **Step 3: Implement**

Add to `src/behelink/db.py`, after `get_link`:

```python
def find_link_by_token_hash(conn: sqlite3.Connection, token_hash: str) -> Link | None:
    row = conn.execute(
        "SELECT * FROM links WHERE owner_token_hash = ? OR resolve_token_hash = ?",
        (token_hash, token_hash),
    ).fetchone()
    return Link(**dict(row)) if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_db.py -v`
Expected: all PASS (including the three new cases)

- [ ] **Step 5: Commit**

```bash
git add src/behelink/db.py tests/test_db.py
git commit -m "feat: add find_link_by_token_hash for reflector token lookup"
```

---

### Task 2: New settings

**Files:**
- Modify: `src/behelink/config.py`

**Interfaces:**
- Produces: six new `Settings` fields (see below) — consumed by Tasks 3–6.

- [ ] **Step 1: Implement (no test file — mirrors existing untested `Settings` fields, exercised
  indirectly via later tasks' tests that override them)**

Replace the body of `src/behelink/config.py`'s `Settings` class with:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEHELINK_")

    database_path: str = "behelink.db"
    heartbeat_ttl_seconds: int = 180
    registration_rate_per_hour: int = 10
    host: str = "127.0.0.1"
    port: int = 47150
    reflector_host: str = "0.0.0.0"
    reflector_port: int = 47151
    reflector_rate_per_minute: int = 20
    reflector_max_payload_bytes: int = 512
    request_connect_rate_per_minute: int = 10
    pending_connect_ttl_seconds: float = 10.0
    pending_connect_wait_max_seconds: float = 25.0
```

- [ ] **Step 2: Verify the existing suite still passes (sanity check — no behavior change yet)**

Run: `uv run pytest -v`
Expected: all PASS (same count as before this task)

- [ ] **Step 3: Commit**

```bash
git add src/behelink/config.py
git commit -m "feat: add hole-punch signaling settings"
```

---

### Task 3: `PendingConnectStore`

**Files:**
- Create: `src/behelink/pending_connect.py`
- Test: `tests/test_pending_connect_store.py`

**Interfaces:**
- Consumes: `behelink.clock.now() -> float` (existing, monkeypatchable time source).
- Produces: `PendingConnect` (dataclass: `ip: str`, `port: int`, `expires_at: float`) and
  `PendingConnectStore(ttl_seconds: float)` with:
  - `put(link_id: str, ip: str, port: int) -> None`
  - `async wait(link_id: str, timeout: float) -> PendingConnect | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pending_connect_store.py`:

```python
import asyncio

from behelink.pending_connect import PendingConnect, PendingConnectStore


async def test_wait_returns_immediately_when_already_queued():
    store = PendingConnectStore(ttl_seconds=10.0)
    store.put("acme-tasks", ip="203.0.113.10", port=41000)
    result = await asyncio.wait_for(store.wait("acme-tasks", timeout=5.0), timeout=1.0)
    assert result == PendingConnect(ip="203.0.113.10", port=41000, expires_at=result.expires_at)


async def test_wait_wakes_up_when_put_arrives_later():
    store = PendingConnectStore(ttl_seconds=10.0)

    async def delayed_put():
        await asyncio.sleep(0.05)
        store.put("acme-tasks", ip="203.0.113.10", port=41000)

    waiter = asyncio.ensure_future(store.wait("acme-tasks", timeout=5.0))
    asyncio.ensure_future(delayed_put())
    result = await asyncio.wait_for(waiter, timeout=1.0)
    assert result is not None
    assert (result.ip, result.port) == ("203.0.113.10", 41000)


async def test_wait_times_out_when_nothing_arrives():
    store = PendingConnectStore(ttl_seconds=10.0)
    result = await asyncio.wait_for(store.wait("acme-tasks", timeout=0.05), timeout=1.0)
    assert result is None


async def test_expired_record_is_not_returned(monkeypatch):
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0)
    store = PendingConnectStore(ttl_seconds=10.0)
    store.put("acme-tasks", ip="203.0.113.10", port=41000)
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0 + 10.1)
    result = await asyncio.wait_for(store.wait("acme-tasks", timeout=0.05), timeout=1.0)
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pending_connect_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'behelink.pending_connect'`

- [ ] **Step 3: Implement**

Create `src/behelink/pending_connect.py`:

```python
"""In-memory pending-connect queue for NAT hole-punch signaling.

Single-instance, disposable by design — a restart losing an in-flight record is fine, the caller
retries. Records live only a few seconds (one hole-punch attempt), unlike the durable `links` table.
"""

import asyncio
from dataclasses import dataclass

from behelink import clock


@dataclass
class PendingConnect:
    ip: str
    port: int
    expires_at: float


class PendingConnectStore:
    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, PendingConnect] = {}
        self._events: dict[str, asyncio.Event] = {}

    def _event_for(self, link_id: str) -> asyncio.Event:
        return self._events.setdefault(link_id, asyncio.Event())

    def _peek(self, link_id: str) -> PendingConnect | None:
        record = self._records.get(link_id)
        if record is None:
            return None
        if clock.now() >= record.expires_at:
            del self._records[link_id]
            return None
        return record

    def put(self, link_id: str, ip: str, port: int) -> None:
        now = clock.now()
        self._records[link_id] = PendingConnect(ip=ip, port=port, expires_at=now + self.ttl_seconds)
        self._event_for(link_id).set()

    async def wait(self, link_id: str, timeout: float) -> PendingConnect | None:
        record = self._peek(link_id)
        if record is not None:
            return record
        event = self._event_for(link_id)
        event.clear()
        # Re-check after clearing: nothing else can run between this line and
        # `await event.wait()` below (no await in between), so if a concurrent
        # `put()` landed between the first peek and this line, it's caught here
        # instead of being lost to the clear().
        record = self._peek(link_id)
        if record is not None:
            return record
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return None
        return self._peek(link_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pending_connect_store.py -v`
Expected: all 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/behelink/pending_connect.py tests/test_pending_connect_store.py
git commit -m "feat: add in-memory PendingConnectStore"
```

---

### Task 4: `ReflectorProtocol`

**Files:**
- Create: `src/behelink/reflector.py`
- Test: `tests/test_reflector.py`

**Interfaces:**
- Consumes: `db.connect(path) -> sqlite3.Connection`, `db.find_link_by_token_hash`,
  `tokens.hash_token`, `ratelimit.RateLimiter.allow(key) -> bool`.
- Produces: `ReflectorProtocol(database_path: str, rate_limiter: RateLimiter, max_payload_bytes:
  int)` implementing `asyncio.DatagramProtocol` — `connection_made(transport)`,
  `datagram_received(data, addr)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reflector.py`:

```python
import json

import pytest

from behelink import db, tokens
from behelink.ratelimit import RateLimiter
from behelink.reflector import ReflectorProtocol


class FakeTransport:
    def __init__(self):
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))


@pytest.fixture
def protocol(tmp_path):
    db_path = str(tmp_path / "reflector.db")
    conn = db.connect(db_path)
    owner_token = "blo_ownertokenexample"
    resolve_token = "blr_resolvetokenexample"
    conn.execute(
        "INSERT INTO links (id, port, ip, owner_token_hash, resolve_token_hash, created_at,"
        " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "acme-tasks",
            47130,
            "198.51.100.7",
            tokens.hash_token(owner_token),
            tokens.hash_token(resolve_token),
            1000.0,
            1000.0,
        ),
    )
    conn.commit()
    conn.close()
    proto = ReflectorProtocol(db_path, RateLimiter(20, window_seconds=60.0), max_payload_bytes=512)
    transport = FakeTransport()
    proto.connection_made(transport)
    proto._owner_token = owner_token
    proto._resolve_token = resolve_token
    return proto, transport


def test_valid_owner_token_gets_echoed_address(protocol):
    proto, transport = protocol
    payload = json.dumps({"token": proto._owner_token}).encode()
    proto.datagram_received(payload, ("203.0.113.10", 41000))
    assert transport.sent == [
        (json.dumps({"ip": "203.0.113.10", "port": 41000}).encode(), ("203.0.113.10", 41000))
    ]


def test_valid_resolve_token_gets_echoed_address(protocol):
    proto, transport = protocol
    payload = json.dumps({"token": proto._resolve_token}).encode()
    proto.datagram_received(payload, ("203.0.113.11", 41001))
    assert transport.sent == [
        (json.dumps({"ip": "203.0.113.11", "port": 41001}).encode(), ("203.0.113.11", 41001))
    ]


def test_unknown_token_gets_no_reply(protocol):
    proto, transport = protocol
    payload = json.dumps({"token": "blr_notreal"}).encode()
    proto.datagram_received(payload, ("203.0.113.10", 41000))
    assert transport.sent == []


def test_malformed_json_gets_no_reply(protocol):
    proto, transport = protocol
    proto.datagram_received(b"not json", ("203.0.113.10", 41000))
    assert transport.sent == []


def test_oversized_payload_gets_no_reply(protocol):
    proto, transport = protocol
    payload = json.dumps({"token": proto._owner_token + "x" * 1000}).encode()
    proto.datagram_received(payload, ("203.0.113.10", 41000))
    assert transport.sent == []


def test_rate_limited_ip_gets_no_reply(tmp_path):
    db_path = str(tmp_path / "reflector.db")
    conn = db.connect(db_path)
    owner_token = "blo_ownertokenexample"
    conn.execute(
        "INSERT INTO links (id, port, ip, owner_token_hash, resolve_token_hash, created_at,"
        " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("acme-tasks", 47130, "198.51.100.7", tokens.hash_token(owner_token), "r" * 64, 1000.0, 1000.0),
    )
    conn.commit()
    conn.close()
    proto = ReflectorProtocol(db_path, RateLimiter(1, window_seconds=60.0), max_payload_bytes=512)
    transport = FakeTransport()
    proto.connection_made(transport)
    payload = json.dumps({"token": owner_token}).encode()
    proto.datagram_received(payload, ("203.0.113.10", 41000))
    proto.datagram_received(payload, ("203.0.113.10", 41000))
    assert len(transport.sent) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reflector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'behelink.reflector'`

- [ ] **Step 3: Implement**

Create `src/behelink/reflector.py`:

```python
"""UDP self-STUN reflector: echoes the observed (ip, port) back to a token-bearing prober.

Not RFC 5389 STUN wire-format — only behetask-server/behetask-cli speak to this, so the wire
shape is a minimal JSON envelope. A bad/missing/unrecognized token gets no reply at all (same
"no signal to an invalid credential" posture as the HTTP side's 404-for-invisible-resources).
"""

import asyncio
import json

from behelink import db, tokens
from behelink.ratelimit import RateLimiter


class ReflectorProtocol(asyncio.DatagramProtocol):
    def __init__(self, database_path: str, rate_limiter: RateLimiter, max_payload_bytes: int):
        self._database_path = database_path
        self._rate_limiter = rate_limiter
        self._max_payload_bytes = max_payload_bytes
        self._conn: object = None
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport
        self._conn = db.connect(self._database_path)

    def connection_lost(self, exc: Exception | None) -> None:
        if self._conn is not None:
            self._conn.close()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) > self._max_payload_bytes:
            return
        if not self._rate_limiter.allow(addr[0]):
            return
        try:
            payload = json.loads(data.decode("utf-8"))
            token = payload["token"]
            if not isinstance(token, str):
                return
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return
        link = db.find_link_by_token_hash(self._conn, tokens.hash_token(token))
        if link is None:
            return
        reply = json.dumps({"ip": addr[0], "port": addr[1]}).encode("utf-8")
        self.transport.sendto(reply, addr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reflector.py -v`
Expected: all 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/behelink/reflector.py tests/test_reflector.py
git commit -m "feat: add UDP self-STUN reflector protocol"
```

---

### Task 5: Wire the reflector into `create_app` via lifespan

**Files:**
- Modify: `src/behelink/main.py`

**Interfaces:**
- Consumes: `ReflectorProtocol` (Task 4), `Settings.reflector_host/port/rate_per_minute/max_payload_bytes` (Task 2).
- Produces: `app.state.reflector_rate_limiter`, `app.state.request_connect_limiter`,
  `app.state.pending_connect_store` — consumed by Tasks 6–7. The real UDP socket only binds when
  the app is actually served (lifespan), never during `create_app()` itself or in tests that don't
  invoke ASGI lifespan (the existing `tests/conftest.py` `client` fixture does not).

- [ ] **Step 1: Implement**

In `src/behelink/main.py`, add imports (top of file, alongside existing ones):

```python
from contextlib import asynccontextmanager

from behelink.pending_connect import PendingConnectStore
from behelink.reflector import ReflectorProtocol
```

Replace the `create_app` function's opening (from `def create_app` through the `app.state.rate_limiter` line) with:

```python
@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: ReflectorProtocol(
            settings.database_path,
            app.state.reflector_rate_limiter,
            settings.reflector_max_payload_bytes,
        ),
        local_addr=(settings.reflector_host, settings.reflector_port),
    )
    try:
        yield
    finally:
        transport.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="behelink", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.state.settings = settings
    install_handlers(app)
    app.state.rate_limiter = RateLimiter(settings.registration_rate_per_hour)
    app.state.reflector_rate_limiter = RateLimiter(
        settings.reflector_rate_per_minute, window_seconds=60.0
    )
    app.state.request_connect_limiter = RateLimiter(
        settings.request_connect_rate_per_minute, window_seconds=60.0
    )
    app.state.pending_connect_store = PendingConnectStore(settings.pending_connect_ttl_seconds)
```

Add `import asyncio` to the top-level imports (alongside `import re`, `import sqlite3`).

Leave everything from `def get_conn():` onward in `create_app` unchanged for this task — Tasks 6–7
add the new routes before the final `return app`.

- [ ] **Step 2: Run the full existing suite to confirm nothing broke**

Run: `uv run pytest -v`
Expected: all PASS, same count as before this task (lifespan is inert for every existing test —
none of them trigger ASGI lifespan startup)

- [ ] **Step 3: Commit**

```bash
git add src/behelink/main.py
git commit -m "feat: wire UDP reflector lifespan and new rate limiters into create_app"
```

---

### Task 6: `POST /v1/links/{id}:requestConnect`

**Files:**
- Modify: `src/behelink/main.py`
- Test: `tests/test_request_connect.py`

**Interfaces:**
- Consumes: `app.state.pending_connect_store.put(link_id, ip, port)`,
  `app.state.request_connect_limiter.allow(key) -> bool`, existing `_bearer_token`, `_not_found`,
  `_client_ip`, `db.get_link`, `tokens.verify_token`.
- Produces: route `POST /v1/links/{link_id}:requestConnect` → `200 {"ip": str, "port": int}` (the
  link's own current candidate) or `404`/`401`/`429`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_request_connect.py`:

```python
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
```

(`CLIENT_IP` from `tests/conftest.py` is `203.0.113.10`, used implicitly via the `client` fixture.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_request_connect.py -v`
Expected: FAIL with 404s from FastAPI (route doesn't exist yet)

- [ ] **Step 3: Implement**

In `src/behelink/main.py`, add a request model near `HeartbeatRequest`:

```python
class RequestConnectRequest(BaseModel):
    port: int = Field(ge=1, le=65535)
```

Add the route inside `create_app`, after `rotate_resolve_token` and before `return app`:

```python
    @app.post("/v1/links/{link_id}:requestConnect")
    def request_connect(
        link_id: str,
        body: RequestConnectRequest,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, object]:
        token = _bearer_token(request)
        link = db.get_link(conn, link_id)
        if link is None or not tokens.verify_token(token, link.resolve_token_hash):
            raise _not_found(link_id)
        if not app.state.request_connect_limiter.allow(token):
            raise ProblemError(
                429,
                "rate_limited",
                "Too Many Requests",
                "connect-request rate limit exceeded for this token",
                headers={"Retry-After": "60"},
            )
        app.state.pending_connect_store.put(link_id, ip=_client_ip(request), port=body.port)
        return {"ip": link.ip, "port": link.port}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_request_connect.py -v`
Expected: all 6 PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/behelink/main.py tests/test_request_connect.py
git commit -m "feat: add POST /v1/links/{id}:requestConnect"
```

---

### Task 7: `GET /v1/links/{id}/pending-connect`

**Files:**
- Modify: `src/behelink/main.py`
- Test: `tests/test_pending_connect_endpoint.py`

**Interfaces:**
- Consumes: `app.state.pending_connect_store.wait(link_id, timeout) -> PendingConnect | None`,
  `Settings.pending_connect_wait_max_seconds`.
- Produces: route `GET /v1/links/{link_id}/pending-connect?wait=N` → `200 {"ip": str, "port": int}`
  if a record is available (immediately or after waking), else `204` after the clamped wait; `404`
  for an unknown/wrong-token link.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pending_connect_endpoint.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pending_connect_endpoint.py -v`
Expected: FAIL with 404s (route doesn't exist yet)

- [ ] **Step 3: Implement**

In `src/behelink/main.py`, add `Query` to the `fastapi` import line and `JSONResponse` to a new
import:

```python
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
```

Add the route inside `create_app`, after `request_connect` and before `return app`:

```python
    @app.get("/v1/links/{link_id}/pending-connect")
    async def pending_connect(
        link_id: str,
        request: Request,
        wait: float = Query(default=0.0, ge=0.0),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        token = _bearer_token(request)
        link = db.get_link(conn, link_id)
        if link is None or not tokens.verify_token(token, link.owner_token_hash):
            raise _not_found(link_id)
        clamped_wait = min(wait, settings.pending_connect_wait_max_seconds)
        record = await app.state.pending_connect_store.wait(link_id, timeout=clamped_wait)
        if record is None:
            return Response(status_code=204)
        return JSONResponse({"ip": record.ip, "port": record.port})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pending_connect_endpoint.py -v`
Expected: all 7 PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/behelink/main.py tests/test_pending_connect_endpoint.py
git commit -m "feat: add GET /v1/links/{id}/pending-connect long-poll"
```

---

### Task 8: Documentation — README and deploy runbook

**Files:**
- Modify: `README.md`
- Modify: `deploy.md`

**Interfaces:** None (documentation only).

- [ ] **Step 1: Extend README's API table**

In `README.md`, after the `POST /v1/links/{id}:rotateResolveToken` row, add:

```markdown
| `POST /v1/links/{id}:requestConnect` `{port}` | Bearer `resolve_token` | queues a pending-connect (short TTL); `200 {ip, port}` — the link's own current candidate, same round trip |
| `GET /v1/links/{id}/pending-connect?wait=N` | Bearer `owner_token` | long-poll; `200 {ip, port}` once a connect request lands, else `204` after `min(N, BEHELINK_PENDING_CONNECT_WAIT_MAX_SECONDS)` |
```

Add a sentence after the existing "A link is **stale**..." paragraph:

```markdown
For hole-punch usage (behetask Mode D), `port` in `PATCH`/`POST /v1/links` means the caller's
self-STUN'd candidate port, not a forwarded listening port — self-STUN against the UDP reflector
below. `ip` is still always server-observed, never client-declared, on every endpoint including
the two above.

behelink also runs a UDP self-STUN reflector on `BEHELINK_REFLECTOR_PORT`: send `{"token": "<owner_token
or resolve_token>"}` as JSON; a valid token gets `{"ip", "port"}` echoing the observed source
address back. An invalid/missing token gets no reply.
```

Extend the Configuration table with:

```markdown
| `BEHELINK_REFLECTOR_HOST` | `0.0.0.0` | UDP reflector bind (no Caddy in front — see `deploy.md`) |
| `BEHELINK_REFLECTOR_PORT` | `47151` | UDP reflector port (umbrella port registry) |
| `BEHELINK_REFLECTOR_RATE_PER_MINUTE` | `20` | per-source-IP reflector probe limit |
| `BEHELINK_REFLECTOR_MAX_PAYLOAD_BYTES` | `512` | oversized probes are dropped, no reply |
| `BEHELINK_REQUEST_CONNECT_RATE_PER_MINUTE` | `10` | per-resolve_token `:requestConnect` limit |
| `BEHELINK_PENDING_CONNECT_TTL_SECONDS` | `10.0` | how long a queued connect request stays valid |
| `BEHELINK_PENDING_CONNECT_WAIT_MAX_SECONDS` | `25.0` | server-side clamp on the long-poll `wait` query param |
```

- [ ] **Step 2: Extend deploy.md**

In `deploy.md`, add a new subsection after "## Ops" (before "## Live verification"):

```markdown
## Hole-punch signaling (not yet applied to the live box)

Adds a UDP listener on `BEHELINK_REFLECTOR_PORT` (default `47151`) in the same process — no new
systemd unit, no Caddy change (Caddy can't front raw UDP). Before deploying this:

- `sudo ufw allow 47151/udp` on the live box (mirrors the existing `allow 443/udp` rule) — a
  manual step for the operator, run once when ready to ship this feature, not part of an automated
  redeploy.
- Confirm `BEHELINK_REFLECTOR_HOST=0.0.0.0` (the default) — unlike the HTTP listener, this one
  binds a public interface directly, by design (Caddy can't proxy it).
- After the `ufw` rule is live, verify with `nc -u 46.17.103.230 47151` sending
  `{"token":"<a live token>"}` from an outside network and confirming a JSON reply.
```

- [ ] **Step 3: Commit**

```bash
git add README.md deploy.md
git commit -m "docs: document hole-punch signaling API, config, and the new UDP port's deploy step"
```

---

### Task 9: `HARNESS-DIVERGENCES.md` entry

**Files:**
- Modify: `HARNESS-DIVERGENCES.md`

**Interfaces:** None (documentation only).

- [ ] **Step 1: Add divergence entry 2**

Append to the "## Divergences" section (after divergence 1) in `HARNESS-DIVERGENCES.md`:

```markdown
2. **New public, directly-exposed UDP port (not Caddy-fronted).** The UDP self-STUN reflector
   (`src/behelink/reflector.py`) binds `BEHELINK_REFLECTOR_HOST:BEHELINK_REFLECTOR_PORT` (default
   `0.0.0.0:47151`) directly — Caddy can't proxy raw UDP the way this needs, so unlike every other
   public surface today, this listener answers straight from the OS socket. Design and the
   behelink-owner sign-off on this trade-off:
   `docs/superpowers/specs/2026-07-21-behelink-holepunch-signaling-design.md` (Security
   Considerations). Mitigations: bearer-token-gated probes (no anonymous reflection), a
   per-source-IP rate limiter, and a request/reply size ratio that defeats classic UDP
   amplification abuse by construction.
```

- [ ] **Step 2: Commit**

```bash
git add HARNESS-DIVERGENCES.md
git commit -m "docs: record the hole-punch reflector's UDP-exposure divergence"
```

---

### Task 10: Umbrella port registry (separate git repo)

**Files (in the BEHEMOTION umbrella repo, one directory up from `behelink/` — a different git
root; commit there, not in behelink):**
- Modify: `../docs/CONVENTIONS.md`
- Modify: `../docs/HARNESS-PLAN.md`

**Interfaces:** None (documentation only). Do this task from `../` (the umbrella root), not from
inside `behelink/`.

- [ ] **Step 1: Update the port registry line in `CONVENTIONS.md`**

In `../docs/CONVENTIONS.md`, find the "Port registry" bullet under "## 7. Service packaging &
deploy" and change:

```
- **Port registry** (dev-4): `behemcp 47100 · behelib 47110/db 47119 ·
  behemem 47120/db 47129 · behetask 47130/db 47139 · behedaemon 47140/db
  47149 · behelink 47150`. Code defaults, compose files, and deploy docs MUST
```

to:

```
- **Port registry** (dev-4): `behemcp 47100 · behelib 47110/db 47119 ·
  behemem 47120/db 47129 · behetask 47130/db 47139 · behedaemon 47140/db
  47149 · behelink 47150/udp-reflector 47151`. Code defaults, compose files, and deploy docs MUST
```

- [ ] **Step 2: Update the per-repo table note in `HARNESS-PLAN.md`**

In `../docs/HARNESS-PLAN.md`, find the `behelink` row in the "## Per-repo index" table:

```
| `behelink`              | [`../behelink/HARNESS-DIVERGENCES.md`](../behelink/HARNESS-DIVERGENCES.md) | deliberately public-facing (NAT rendezvous, spec-approved); added post-audit, live at `link.behemotion.com` since 2026-07-21 |
```

and change it to:

```
| `behelink`              | [`../behelink/HARNESS-DIVERGENCES.md`](../behelink/HARNESS-DIVERGENCES.md) | deliberately public-facing (NAT rendezvous, spec-approved); added post-audit, live at `link.behemotion.com` since 2026-07-21; hole-punch signaling adds a second, directly-exposed UDP port (47151) — see behelink's divergence #2 |
```

- [ ] **Step 3: Commit — from the umbrella root, not from `behelink/`**

```bash
cd /Users/alexandr/Repo/BEHEMOTION
git add docs/CONVENTIONS.md docs/HARNESS-PLAN.md
git commit -m "docs: register behelink's UDP reflector port (47151) in the port registry"
```

---

## Post-plan (not part of this plan's tasks — flagged, not scheduled)

- Applying the `ufw allow 47151/udp` rule and redeploying to the live box (`46.17.103.230`) is a
  manual operator action once this code is reviewed and merged — see the new `deploy.md` section
  from Task 8. Do not run this automatically as part of executing this plan.
- behetask's own implementation (self-STUN, the restructured relay task, the QUIC/HTTP-3 listener,
  the CLI's new resolve step) is out of scope here entirely — tracked in behetask's own repo per
  its design spec.
