# behelink Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the behelink hosted NAT rendezvous service — register/heartbeat/resolve `{ip, port}` for NAT'd behetask-servers — per `docs/superpowers/specs/2026-07-21-behelink-hosted-relay-design.md`.

**Architecture:** Single FastAPI app, stdlib `sqlite3` single-table storage, sync endpoint functions (FastAPI runs them in its threadpool), connection-per-request. Tokens are CSPRNG secrets hashed with unsalted SHA-256 (behetask API-key precedent). Observed client IP comes from `request.client.host`; in production uvicorn runs with `proxy_headers=True, forwarded_allow_ips="127.0.0.1"` so Caddy's `X-Forwarded-For` is honored transparently.

**Tech Stack:** Python ≥3.12, FastAPI, uvicorn, pydantic-settings, stdlib sqlite3, pytest + pytest-asyncio + httpx (`ASGITransport` against the test app), uv for env management, hatchling build backend.

## Global Constraints

- Env prefix `BEHELINK_`, no exceptions (umbrella `docs/CONVENTIONS.md` §6).
- Service port **47150**, listener binds `127.0.0.1` (Caddy fronts it publicly).
- HTTP API under bare `/v1`; errors are RFC 9457 `application/problem+json` `{type, title, status, detail?}`; `type` uses underscore vocabulary (`not_found`, `auth`, `conflict`, `validation_error`, `rate_limited`, `internal`).
- `GET /healthz` → `200 {"status": "ok"}`, unauthenticated, DB ping only.
- Bearer auth on every `/v1` surface; missing/malformed credentials → **401** + `WWW-Authenticate: Bearer`. A presented-but-wrong token is answered **404**, identical to a nonexistent link (invisible-resource rule, closes ID enumeration; documented in README).
- Token compare is constant-time (`hmac.compare_digest`).
- Dependency floor: no bcrypt/pyjwt/etc — stdlib `hashlib`/`secrets` only for crypto.
- Python ≥ 3.12; `pyproject.toml` name = `behelink`, single-sourced version.
- Staleness: link is offline when `now - last_seen > BEHELINK_HEARTBEAT_TTL_SECONDS` (default **180** = 3 × 60s heartbeat).
- Registration rate limit: `BEHELINK_REGISTRATION_RATE_PER_HOUR` (default **10**) per source IP on `POST /v1/links`.
- Link IDs: `^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$` and length ≥ 3 (lowercase DNS-label shape; colon excluded so AIP `:rotateResolveToken` routing can't collide).
- Commit after every task; work directly on `main` of the `behelink` repo (fresh repo, design commit only).

## File Structure

- `pyproject.toml` — packaging, deps, pytest config
- `src/behelink/__init__.py` — version string
- `src/behelink/config.py` — `Settings` (pydantic-settings, `BEHELINK_` prefix)
- `src/behelink/clock.py` — `now()` indirection so tests control time
- `src/behelink/tokens.py` — generate/hash/verify
- `src/behelink/db.py` — sqlite connect + schema + CRUD on `links`
- `src/behelink/errors.py` — `ProblemError` + RFC 9457 handlers
- `src/behelink/ratelimit.py` — in-memory per-key sliding-window limiter
- `src/behelink/main.py` — `create_app()` app factory + routes
- `src/behelink/__main__.py` — uvicorn entrypoint (`behelink` console script)
- `tests/conftest.py` — app/client fixtures (tmp DB, fixed client IP)
- `tests/test_healthz.py`, `tests/test_errors.py`, `tests/test_tokens.py`, `tests/test_db.py`, `tests/test_register.py`, `tests/test_heartbeat.py`, `tests/test_resolve.py`, `tests/test_manage.py`, `tests/test_ratelimit.py`
- `README.md` (update), `deploy.md`, `HARNESS-DIVERGENCES.md`, `.gitignore`

---

### Task 1: Scaffolding, settings, DB bootstrap, `/healthz`

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/behelink/__init__.py`, `src/behelink/config.py`, `src/behelink/clock.py`, `src/behelink/db.py` (connect + schema only), `src/behelink/main.py` (factory + healthz only)
- Test: `tests/conftest.py`, `tests/test_healthz.py`

**Interfaces:**
- Produces: `Settings(database_path, heartbeat_ttl_seconds, registration_rate_per_hour, host, port)`; `db.connect(path) -> sqlite3.Connection` (schema applied, WAL, `sqlite3.Row` factory); `create_app(settings: Settings | None = None) -> FastAPI` with `app.state.settings`; `clock.now() -> float`.

- [ ] **Step 1: Write project files**

`pyproject.toml`:
```toml
[project]
name = "behelink"
version = "0.1.0"
description = "Hosted NAT rendezvous service for the BEHEMOTION harness"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.111",
  "uvicorn[standard]>=0.30",
  "pydantic>=2.7",
  "pydantic-settings>=2.3",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "anyio>=4", "httpx>=0.27"]

[project.scripts]
behelink = "behelink.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
testpaths = ["tests"]
```

`.gitignore`:
```
__pycache__/
*.py[cod]
.venv/
*.db
*.db-wal
*.db-shm
.pytest_cache/
dist/
```

`src/behelink/__init__.py`:
```python
"""behelink — hosted NAT rendezvous service for the BEHEMOTION harness."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("behelink")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"
```

`src/behelink/config.py`:
```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEHELINK_")

    database_path: str = "behelink.db"
    heartbeat_ttl_seconds: int = 180
    registration_rate_per_hour: int = 10
    host: str = "127.0.0.1"
    port: int = 47150
```

`src/behelink/clock.py`:
```python
"""Time source indirection — tests monkeypatch behelink.clock.now."""

import time


def now() -> float:
    return time.time()
```

`src/behelink/db.py`:
```python
"""SQLite storage: single `links` table, connection-per-request."""

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    port INTEGER NOT NULL,
    ip TEXT NOT NULL,
    owner_token_hash TEXT NOT NULL,
    resolve_token_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
)
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    conn.commit()
    return conn
```

`src/behelink/main.py`:
```python
from collections.abc import Iterator

import sqlite3

from fastapi import Depends, FastAPI

from behelink import db
from behelink.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="behelink", docs_url=None, redoc_url=None)
    app.state.settings = settings

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.database_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/healthz")
    def healthz(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    return app
```

- [ ] **Step 2: Write the failing test**

`tests/conftest.py`:
```python
import httpx
import pytest

from behelink.config import Settings
from behelink.main import create_app

CLIENT_IP = "203.0.113.10"


@pytest.fixture
def app(tmp_path):
    settings = Settings(database_path=str(tmp_path / "behelink.db"))
    return create_app(settings)


def make_client(app, ip: str = CLIENT_IP) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 41000))
    return httpx.AsyncClient(transport=transport, base_url="http://behelink.test")


@pytest.fixture
async def client(app):
    async with make_client(app) as c:
        yield c
```

`tests/test_healthz.py`:
```python
async def test_healthz_ok(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 3: Create env and run test to verify it fails before install / passes after**

Run: `uv sync --extra dev` then `uv run pytest tests/test_healthz.py -v`
Expected: PASS (scaffolding and test land together; the meaningful failure gate applies from Task 2 on, where every endpoint test is written and run red first)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml .gitignore src tests uv.lock
git commit -m "feat: scaffold behelink FastAPI app with settings, sqlite bootstrap, /healthz"
```

---

### Task 2: RFC 9457 error module + handlers

**Files:**
- Create: `src/behelink/errors.py`
- Modify: `src/behelink/main.py` (install handlers in `create_app`)
- Test: `tests/test_errors.py`

**Interfaces:**
- Produces: `ProblemError(status, type_, title, detail=None, headers=None)` exception; `install_handlers(app)`; every error response body is `{type, title, status, detail?}` with `content-type: application/problem+json`.

- [ ] **Step 1: Write the failing tests**

`tests/test_errors.py`:
```python
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
```

Note: the second test also exercises Task 4's route existing; at this task's point `POST /v1/links` doesn't exist yet, so it returns 404 problem+json — write the test asserting 422 and mark it `@pytest.mark.xfail(reason="route lands in register task", strict=True)`, un-xfail in Task 4. Alternatively keep only the first test now and add the second in Task 4; choose the latter if xfail bookkeeping feels heavier than it's worth.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_errors.py -v`
Expected: FAIL — unknown route currently returns FastAPI's default `{"detail": "Not Found"}` JSON, not problem+json.

- [ ] **Step 3: Implement**

`src/behelink/errors.py`:
```python
"""RFC 9457 application/problem+json errors."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_TYPE_BY_STATUS = {
    400: "usage",
    401: "auth",
    403: "auth",
    404: "not_found",
    405: "usage",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
}


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        type_: str,
        title: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(detail or title)
        self.status = status
        self.type = type_
        self.title = title
        self.detail = detail
        self.headers = headers


def problem_response(
    status: int,
    type_: str,
    title: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {"type": type_, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(
        body,
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(exc.status, exc.type, exc.title, exc.detail, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        type_ = _TYPE_BY_STATUS.get(exc.status_code, "internal")
        return problem_response(exc.status_code, type_, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        return problem_response(422, "validation_error", "Validation Error", detail)
```

In `src/behelink/main.py`, inside `create_app` right after `app.state.settings = settings`:
```python
    install_handlers(app)
```
with import `from behelink.errors import install_handlers`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS (xfail'd test, if kept, reports XFAIL)

- [ ] **Step 5: Commit**

```bash
git add src/behelink/errors.py src/behelink/main.py tests/test_errors.py
git commit -m "feat: RFC 9457 problem+json error envelope and handlers"
```

---

### Task 3: Token helpers + DB CRUD

**Files:**
- Create: `src/behelink/tokens.py`
- Modify: `src/behelink/db.py` (add `Link` dataclass + CRUD)
- Test: `tests/test_tokens.py`, `tests/test_db.py`

**Interfaces:**
- Produces: `tokens.generate_token(prefix: str) -> str` (`{prefix}_{43 alnum chars}`); `tokens.hash_token(token: str) -> str` (sha256 hex); `tokens.verify_token(token: str, token_hash: str) -> bool` (constant-time). `db.Link` dataclass (`id, port, ip, owner_token_hash, resolve_token_hash, created_at, last_seen`); `db.insert_link(conn, link)` (raises `sqlite3.IntegrityError` on dup id); `db.get_link(conn, link_id) -> Link | None`; `db.update_heartbeat(conn, link_id, ip, port, last_seen)`; `db.update_resolve_token_hash(conn, link_id, resolve_token_hash)`; `db.delete_link(conn, link_id)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_tokens.py`:
```python
from behelink import tokens


def test_generate_token_shape():
    tok = tokens.generate_token("blo")
    assert tok.startswith("blo_")
    assert len(tok) == 4 + 43
    assert tok[4:].isalnum()


def test_generate_token_unique():
    assert tokens.generate_token("blr") != tokens.generate_token("blr")


def test_hash_and_verify_roundtrip():
    tok = tokens.generate_token("blo")
    h = tokens.hash_token(tok)
    assert len(h) == 64
    assert tokens.verify_token(tok, h)
    assert not tokens.verify_token("blo_wrong", h)
```

`tests/test_db.py`:
```python
import sqlite3

import pytest

from behelink import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def make_link(link_id="alpha", ts=1000.0):
    return db.Link(
        id=link_id,
        port=47130,
        ip="198.51.100.7",
        owner_token_hash="o" * 64,
        resolve_token_hash="r" * 64,
        created_at=ts,
        last_seen=ts,
    )


def test_insert_and_get(conn):
    db.insert_link(conn, make_link())
    got = db.get_link(conn, "alpha")
    assert got == make_link()


def test_get_missing_returns_none(conn):
    assert db.get_link(conn, "nope") is None


def test_duplicate_id_raises(conn):
    db.insert_link(conn, make_link())
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_link(conn, make_link())


def test_update_heartbeat(conn):
    db.insert_link(conn, make_link())
    db.update_heartbeat(conn, "alpha", ip="203.0.113.99", port=8080, last_seen=2000.0)
    got = db.get_link(conn, "alpha")
    assert (got.ip, got.port, got.last_seen) == ("203.0.113.99", 8080, 2000.0)
    assert got.created_at == 1000.0


def test_update_resolve_token_hash(conn):
    db.insert_link(conn, make_link())
    db.update_resolve_token_hash(conn, "alpha", "n" * 64)
    assert db.get_link(conn, "alpha").resolve_token_hash == "n" * 64


def test_delete_link(conn):
    db.insert_link(conn, make_link())
    db.delete_link(conn, "alpha")
    assert db.get_link(conn, "alpha") is None
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_tokens.py tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: behelink.tokens` / `AttributeError: module 'behelink.db' has no attribute 'Link'`

- [ ] **Step 3: Implement**

`src/behelink/tokens.py`:
```python
"""Token generation and hashing.

Unsalted SHA-256 over a server-generated CSPRNG secret — same pattern as
behetask API keys; safe because the input is already high-entropy.
"""

import hashlib
import hmac
import secrets
import string

_ALPHABET = string.ascii_letters + string.digits
_BODY_LEN = 43


def generate_token(prefix: str) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_BODY_LEN))
    return f"{prefix}_{body}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)
```

Append to `src/behelink/db.py` (plus `from dataclasses import dataclass` at top):
```python
@dataclass
class Link:
    id: str
    port: int
    ip: str
    owner_token_hash: str
    resolve_token_hash: str
    created_at: float
    last_seen: float


def insert_link(conn: sqlite3.Connection, link: Link) -> None:
    conn.execute(
        "INSERT INTO links (id, port, ip, owner_token_hash, resolve_token_hash,"
        " created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            link.id,
            link.port,
            link.ip,
            link.owner_token_hash,
            link.resolve_token_hash,
            link.created_at,
            link.last_seen,
        ),
    )
    conn.commit()


def get_link(conn: sqlite3.Connection, link_id: str) -> Link | None:
    row = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    return Link(**dict(row)) if row else None


def update_heartbeat(
    conn: sqlite3.Connection, link_id: str, ip: str, port: int, last_seen: float
) -> None:
    conn.execute(
        "UPDATE links SET ip = ?, port = ?, last_seen = ? WHERE id = ?",
        (ip, port, last_seen, link_id),
    )
    conn.commit()


def update_resolve_token_hash(
    conn: sqlite3.Connection, link_id: str, resolve_token_hash: str
) -> None:
    conn.execute(
        "UPDATE links SET resolve_token_hash = ? WHERE id = ?",
        (resolve_token_hash, link_id),
    )
    conn.commit()


def delete_link(conn: sqlite3.Connection, link_id: str) -> None:
    conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/behelink/tokens.py src/behelink/db.py tests/test_tokens.py tests/test_db.py
git commit -m "feat: token generation/hashing and links table CRUD"
```

---

### Task 4: `POST /v1/links` — registration

**Files:**
- Modify: `src/behelink/main.py`
- Test: `tests/test_register.py` (+ un-xfail/add the 422 test in `tests/test_errors.py`)

**Interfaces:**
- Consumes: `db.insert_link`, `db.Link`, `tokens.generate_token/hash_token`, `clock.now`, `ProblemError`.
- Produces: route `POST /v1/links` `{id, port}` → `201 {"owner_token": "blo_…", "resolve_token": "blr_…"}`; `409` conflict; `422` invalid id/port. Also module helpers reused later: `_client_ip(request) -> str`, `_ID_RE`, `_iso(ts: float) -> str` (UTC ISO 8601, `Z` suffix).

- [ ] **Step 1: Write the failing tests**

`tests/test_register.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_register.py -v`
Expected: FAIL — 404 problem+json (route doesn't exist yet)

- [ ] **Step 3: Implement**

In `src/behelink/main.py` — add imports and helpers at module level:
```python
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field

from behelink import clock, db, tokens
from behelink.config import Settings
from behelink.errors import ProblemError, install_handlers

_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")


class RegisterRequest(BaseModel):
    id: str
    port: int = Field(ge=1, le=65535)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "0.0.0.0"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _validate_id(link_id: str) -> None:
    if len(link_id) < 3 or not _ID_RE.fullmatch(link_id):
        raise ProblemError(
            422,
            "validation_error",
            "Validation Error",
            "id must be 3-63 chars: lowercase letters, digits, inner hyphens",
        )
```

Inside `create_app`, after the `healthz` route:
```python
    @app.post("/v1/links", status_code=201)
    def register(
        body: RegisterRequest,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, str]:
        _validate_id(body.id)
        owner_token = tokens.generate_token("blo")
        resolve_token = tokens.generate_token("blr")
        now = clock.now()
        link = db.Link(
            id=body.id,
            port=body.port,
            ip=_client_ip(request),
            owner_token_hash=tokens.hash_token(owner_token),
            resolve_token_hash=tokens.hash_token(resolve_token),
            created_at=now,
            last_seen=now,
        )
        try:
            db.insert_link(conn, link)
        except sqlite3.IntegrityError:
            raise ProblemError(
                409, "conflict", "Conflict", f"link id '{body.id}' is already registered"
            )
        return {"owner_token": owner_token, "resolve_token": resolve_token}
```

- [ ] **Step 4: Run full suite (un-xfail the errors 422 test if it was xfail'd)**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/behelink/main.py tests/test_register.py tests/test_errors.py
git commit -m "feat: POST /v1/links registration with claim-and-own tokens"
```

---

### Task 5: Bearer auth helper + `PATCH /v1/links/{id}` heartbeat

**Files:**
- Modify: `src/behelink/main.py`
- Test: `tests/test_heartbeat.py`

**Interfaces:**
- Consumes: Task 4's helpers, `db.get_link`, `db.update_heartbeat`, `tokens.verify_token`, `tests/conftest.make_client`.
- Produces: `_bearer_token(request) -> str` (raises 401 `ProblemError` with `WWW-Authenticate: Bearer` when missing/malformed); `_not_found(link_id)` returning a 404 `ProblemError`; route `PATCH /v1/links/{link_id}` `{port?}` → `200 {"ip", "port", "last_seen"}` (ISO 8601 `last_seen`); wrong/unknown id or wrong owner token → 404.

- [ ] **Step 1: Write the failing tests**

`tests/test_heartbeat.py`:
```python
from conftest import CLIENT_IP, make_client


async def register(client, link_id="acme-tasks", port=47130):
    resp = await client.post("/v1/links", json={"id": link_id, "port": port})
    assert resp.status_code == 201
    return resp.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_heartbeat_refreshes_last_seen_and_ip(app, client, monkeypatch):
    creds = await register(client)
    monkeypatch.setattr("behelink.clock.now", lambda: 5_000_000.0)
    async with make_client(app, ip="198.51.100.99") as roaming:
        resp = await roaming.patch(
            "/v1/links/acme-tasks", json={}, headers=bearer(creds["owner_token"])
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ip"] == "198.51.100.99"
    assert body["port"] == 47130
    assert body["last_seen"] == "1970-02-27T20:53:20Z"


async def test_heartbeat_updates_port(client):
    creds = await register(client)
    resp = await client.patch(
        "/v1/links/acme-tasks", json={"port": 8443}, headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    assert resp.json()["port"] == 8443


async def test_heartbeat_without_token_is_401(client):
    await register(client)
    resp = await client.patch("/v1/links/acme-tasks", json={})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["type"] == "auth"


async def test_heartbeat_wrong_token_is_404(client):
    creds = await register(client)
    for tok in ["blo_definitelywrong", creds["resolve_token"]]:
        resp = await client.patch("/v1/links/acme-tasks", json={}, headers=bearer(tok))
        assert resp.status_code == 404
        assert resp.json()["type"] == "not_found"


async def test_heartbeat_unknown_link_is_404(client):
    resp = await client.patch("/v1/links/ghost", json={}, headers=bearer("blo_x"))
    assert resp.status_code == 404
```

(`1970-02-27T20:53:20Z` is `datetime.fromtimestamp(5_000_000, tz=UTC)` — a fixed value so the ISO formatting is pinned by test.)

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_heartbeat.py -v`
Expected: FAIL — 404/405 (route doesn't exist)

- [ ] **Step 3: Implement**

Module-level additions in `src/behelink/main.py`:
```python
class HeartbeatRequest(BaseModel):
    port: int | None = Field(default=None, ge=1, le=65535)


def _bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise ProblemError(
            401,
            "auth",
            "Unauthorized",
            "missing or malformed bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _not_found(link_id: str) -> ProblemError:
    return ProblemError(
        404, "not_found", "Not Found", f"no resolvable link '{link_id}'"
    )
```

Inside `create_app`:
```python
    @app.patch("/v1/links/{link_id}")
    def heartbeat(
        link_id: str,
        body: HeartbeatRequest,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, object]:
        token = _bearer_token(request)
        link = db.get_link(conn, link_id)
        if link is None or not tokens.verify_token(token, link.owner_token_hash):
            raise _not_found(link_id)
        ip = _client_ip(request)
        port = body.port if body.port is not None else link.port
        now = clock.now()
        db.update_heartbeat(conn, link_id, ip=ip, port=port, last_seen=now)
        return {"ip": ip, "port": port, "last_seen": _iso(now)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/behelink/main.py tests/test_heartbeat.py
git commit -m "feat: PATCH /v1/links/{id} heartbeat with IP re-capture"
```

---

### Task 6: `GET /v1/links/{id}` — resolve with staleness

**Files:**
- Modify: `src/behelink/main.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: `_bearer_token`, `_not_found`, `db.get_link`, `tokens.verify_token`, `settings.heartbeat_ttl_seconds`, `clock.now`.
- Produces: route `GET /v1/links/{link_id}` → `200 {"ip", "port", "last_seen"}`; 401 no token; 404 for unknown id, wrong resolve token, owner token presented, or `now - last_seen > heartbeat_ttl_seconds`.

- [ ] **Step 1: Write the failing tests**

`tests/test_resolve.py`:
```python
from conftest import CLIENT_IP


async def register(client, link_id="acme-tasks", port=47130):
    resp = await client.post("/v1/links", json={"id": link_id, "port": port})
    assert resp.status_code == 201
    return resp.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_resolve_returns_address(client):
    creds = await register(client)
    resp = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ip"] == CLIENT_IP
    assert body["port"] == 47130
    assert body["last_seen"].endswith("Z")


async def test_resolve_without_token_is_401(client):
    await register(client)
    resp = await client.get("/v1/links/acme-tasks")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_resolve_wrong_token_is_404(client):
    creds = await register(client)
    for tok in ["blr_wrong", creds["owner_token"]]:
        resp = await client.get("/v1/links/acme-tasks", headers=bearer(tok))
        assert resp.status_code == 404


async def test_resolve_stale_link_is_404(client, monkeypatch):
    creds = await register(client)
    base = 5_000_000.0
    monkeypatch.setattr("behelink.clock.now", lambda: base)
    resp = await client.patch(
        "/v1/links/acme-tasks", json={}, headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    # 180s TTL: 180s after last heartbeat still fresh, 181s is stale
    monkeypatch.setattr("behelink.clock.now", lambda: base + 180.0)
    fresh = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert fresh.status_code == 200
    monkeypatch.setattr("behelink.clock.now", lambda: base + 181.0)
    stale = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert stale.status_code == 404
    assert stale.json()["type"] == "not_found"


async def test_stale_link_revives_on_heartbeat(client, monkeypatch):
    creds = await register(client)
    monkeypatch.setattr("behelink.clock.now", lambda: 5_000_000.0)
    resp = await client.patch(
        "/v1/links/acme-tasks", json={}, headers=bearer(creds["owner_token"])
    )
    assert resp.status_code == 200
    resp = await client.get("/v1/links/acme-tasks", headers=bearer(creds["resolve_token"]))
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_resolve.py -v`
Expected: FAIL — 405/404 (route doesn't exist)

- [ ] **Step 3: Implement**

Inside `create_app`:
```python
    @app.get("/v1/links/{link_id}")
    def resolve(
        link_id: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, object]:
        token = _bearer_token(request)
        link = db.get_link(conn, link_id)
        if link is None or not tokens.verify_token(token, link.resolve_token_hash):
            raise _not_found(link_id)
        if clock.now() - link.last_seen > settings.heartbeat_ttl_seconds:
            raise _not_found(link_id)
        return {"ip": link.ip, "port": link.port, "last_seen": _iso(link.last_seen)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/behelink/main.py tests/test_resolve.py
git commit -m "feat: GET /v1/links/{id} resolve with staleness cutoff"
```

---

### Task 7: `DELETE /v1/links/{id}` + `POST /v1/links/{id}:rotateResolveToken`

**Files:**
- Modify: `src/behelink/main.py`
- Test: `tests/test_manage.py`

**Interfaces:**
- Consumes: `_bearer_token`, `_not_found`, `db.delete_link`, `db.update_resolve_token_hash`, `tokens.generate_token/hash_token/verify_token`.
- Produces: `DELETE /v1/links/{link_id}` → 204 empty body; `POST /v1/links/{link_id}:rotateResolveToken` → `200 {"resolve_token": "blr_…"}`, old resolve token invalidated. Both owner-token-gated, 404 on wrong token/unknown id.

- [ ] **Step 1: Write the failing tests**

`tests/test_manage.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_manage.py -v`
Expected: FAIL — 405/404 (routes don't exist)

- [ ] **Step 3: Implement**

Add `Response` to the fastapi import. Inside `create_app`:
```python
    @app.delete("/v1/links/{link_id}", status_code=204)
    def deregister(
        link_id: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        token = _bearer_token(request)
        link = db.get_link(conn, link_id)
        if link is None or not tokens.verify_token(token, link.owner_token_hash):
            raise _not_found(link_id)
        db.delete_link(conn, link_id)
        return Response(status_code=204)

    @app.post("/v1/links/{link_id}:rotateResolveToken")
    def rotate_resolve_token(
        link_id: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, str]:
        token = _bearer_token(request)
        link = db.get_link(conn, link_id)
        if link is None or not tokens.verify_token(token, link.owner_token_hash):
            raise _not_found(link_id)
        resolve_token = tokens.generate_token("blr")
        db.update_resolve_token_hash(conn, link_id, tokens.hash_token(resolve_token))
        return {"resolve_token": resolve_token}
```

Route-matching note: Starlette's `{link_id}` regex is greedy `[^/]+` but backtracks to satisfy the `:rotateResolveToken` literal, so the colon-method route matches correctly; plain-id routes can technically capture colon-bearing ids, which then just 404 at lookup (ids can never contain `:` per `_ID_RE`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/behelink/main.py tests/test_manage.py
git commit -m "feat: DELETE and :rotateResolveToken owner operations"
```

---

### Task 8: Registration rate limiting

**Files:**
- Create: `src/behelink/ratelimit.py`
- Modify: `src/behelink/main.py` (wire limiter into `register`)
- Test: `tests/test_ratelimit.py`

**Interfaces:**
- Consumes: `clock.now`, `settings.registration_rate_per_hour`, `_client_ip`.
- Produces: `RateLimiter(limit: int, window_seconds: float = 3600.0)` with `.allow(key: str) -> bool` (sliding log per key); `app.state.rate_limiter`; `POST /v1/links` returns `429 {"type": "rate_limited", …}` + `Retry-After: 3600` once an IP exceeds the limit within the window.

- [ ] **Step 1: Write the failing tests**

`tests/test_ratelimit.py`:
```python
from behelink.ratelimit import RateLimiter
from conftest import make_client


def test_limiter_allows_up_to_limit(monkeypatch):
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0)
    rl = RateLimiter(limit=3, window_seconds=3600.0)
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("b") is True  # other keys unaffected


def test_limiter_window_expires(monkeypatch):
    rl = RateLimiter(limit=1, window_seconds=3600.0)
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0)
    assert rl.allow("a") is True
    assert rl.allow("a") is False
    monkeypatch.setattr("behelink.clock.now", lambda: 1000.0 + 3600.1)
    assert rl.allow("a") is True


async def test_register_rate_limited_per_ip(app):
    async with make_client(app, ip="192.0.2.1") as c:
        for i in range(10):
            resp = await c.post("/v1/links", json={"id": f"link-{i:02d}", "port": 1000 + i})
            assert resp.status_code == 201, i
        resp = await c.post("/v1/links", json={"id": "link-10", "port": 1010})
        assert resp.status_code == 429
        assert resp.json()["type"] == "rate_limited"
        assert resp.headers["retry-after"] == "3600"
    async with make_client(app, ip="192.0.2.2") as other:
        resp = await other.post("/v1/links", json={"id": "link-10", "port": 1010})
        assert resp.status_code == 201
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: FAIL — `ModuleNotFoundError: behelink.ratelimit`

- [ ] **Step 3: Implement**

`src/behelink/ratelimit.py`:
```python
"""In-memory per-key sliding-window rate limiter (single-instance service)."""

from collections import deque

from behelink import clock


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 3600.0):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = clock.now()
        events = self._events.setdefault(key, deque())
        while events and events[0] <= now - self.window_seconds:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True
```

In `create_app`, after `install_handlers(app)`:
```python
    app.state.rate_limiter = RateLimiter(settings.registration_rate_per_hour)
```
(import `from behelink.ratelimit import RateLimiter`).

In `register`, after `_validate_id(body.id)`:
```python
        if not app.state.rate_limiter.allow(_client_ip(request)):
            raise ProblemError(
                429,
                "rate_limited",
                "Too Many Requests",
                "registration rate limit exceeded for this address",
                headers={"Retry-After": "3600"},
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/behelink/ratelimit.py src/behelink/main.py tests/test_ratelimit.py
git commit -m "feat: per-IP rate limit on link registration"
```

---

### Task 9: Entrypoint, README, deploy runbook, divergences doc

**Files:**
- Create: `src/behelink/__main__.py`, `deploy.md`, `HARNESS-DIVERGENCES.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: `create_app`, `Settings`.
- Produces: `behelink` console script / `python -m behelink` starting uvicorn on `settings.host:settings.port` with `proxy_headers=True, forwarded_allow_ips="127.0.0.1"`.

- [ ] **Step 1: Write `src/behelink/__main__.py`**

```python
import uvicorn

from behelink.config import Settings
from behelink.main import create_app


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        # Caddy fronts behelink on the same host; trust its X-Forwarded-For so
        # the observed client IP is the real public address, not 127.0.0.1.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the entrypoint**

Run: `BEHELINK_DATABASE_PATH=/tmp/behelink-smoke.db uv run behelink & sleep 2 && curl -s http://127.0.0.1:47150/healthz && kill %1`
Expected: `{"status":"ok"}`

- [ ] **Step 3: Update `README.md`** — replace the Status section with a Usage overview: API table (6 endpoints, auth column), env vars table (`BEHELINK_DATABASE_PATH`, `BEHELINK_HEARTBEAT_TTL_SECONDS`, `BEHELINK_REGISTRATION_RATE_PER_HOUR`, `BEHELINK_HOST`, `BEHELINK_PORT`), quickstart (`uv sync --extra dev`, `uv run pytest`, `uv run behelink`), and a Security note covering: tokens shown once, wrong-token = 404 by design (enumeration defense), owner-token-compromise = redirection risk only (behetask API auth still gates), behelink stores only `{ip, port, hashes, timestamps}`.

- [ ] **Step 4: Write `deploy.md`** — rootless Podman + systemd-user + Caddy pattern per behetask's `deploy.md`, with the behelink-specific deltas: public vhost (real domain + real TLS, not `*.home.lan`), uvicorn binds `127.0.0.1:47150`, Caddy `reverse_proxy 127.0.0.1:47150`, SQLite file on a mounted volume, `Retry-After`/rate-limit note. Mark the target host "TBD — public server to be provided" since deployment infra isn't known yet.

- [ ] **Step 5: Write `HARNESS-DIVERGENCES.md`** — one audited divergence: network posture (behelink's Caddy vhost is deliberately public-internet-reachable, vs `docs/CONVENTIONS.md` §4 LAN-only assumption; per spec's Security Considerations). Note conformance elsewhere (bare `/v1`, RFC 9457, bearer auth, `BEHELINK_` prefix, `/healthz`, port 47150) and that behelink ships no CLI in v1 (so no beheaxi profile obligations yet).

- [ ] **Step 6: Run full suite and commit**

Run: `uv run pytest -v`
Expected: all PASS

```bash
git add src/behelink/__main__.py README.md deploy.md HARNESS-DIVERGENCES.md
git commit -m "feat: uvicorn entrypoint, README, deploy runbook, divergences doc"
```

---

## Deferred (per spec, not in this plan)

- behetask-server registration/heartbeat background task; behetask-cli resolve step; `network setup` wizard mode — separate repos' work.
- Live T0-style test from a real NAT'd box — requires the public deployment (user to provide the server).
- Umbrella follow-up: add `behelink 47150` to the port registry in `docs/CONVENTIONS.md` §7 / `HARNESS-PLAN.md` (umbrella-owned files — flag via handoff, don't fork).
- behelink ops CLI, per-user resolve tokens, HA — Open Follow-Ups in the spec.
