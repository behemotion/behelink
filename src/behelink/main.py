import asyncio
import re
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, Field

from behelink import clock, db, tokens
from behelink.config import Settings
from behelink.errors import ProblemError, install_handlers
from behelink.pending_connect import PendingConnectStore
from behelink.ratelimit import RateLimiter
from behelink.reflector import ReflectorProtocol

_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")


class RegisterRequest(BaseModel):
    id: str
    port: int = Field(ge=1, le=65535)


class HeartbeatRequest(BaseModel):
    port: int | None = Field(default=None, ge=1, le=65535)


class RequestConnectRequest(BaseModel):
    port: int = Field(ge=1, le=65535)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "0.0.0.0"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


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


def _validate_id(link_id: str) -> None:
    if len(link_id) < 3 or not _ID_RE.fullmatch(link_id):
        raise ProblemError(
            422,
            "validation_error",
            "Validation Error",
            "id must be 3-63 chars: lowercase letters, digits, inner hyphens",
        )


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

    @app.post("/v1/links", status_code=201)
    def register(
        body: RegisterRequest,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, str]:
        _validate_id(body.id)
        if not app.state.rate_limiter.allow(_client_ip(request)):
            raise ProblemError(
                429,
                "rate_limited",
                "Too Many Requests",
                "registration rate limit exceeded for this address",
                headers={"Retry-After": "3600"},
            )
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

    return app
