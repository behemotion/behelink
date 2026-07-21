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


class HeartbeatRequest(BaseModel):
    port: int | None = Field(default=None, ge=1, le=65535)


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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="behelink", docs_url=None, redoc_url=None)
    app.state.settings = settings
    install_handlers(app)

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

    return app
