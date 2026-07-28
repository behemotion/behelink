import asyncio
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from behelink import clock, db, tokens
from behelink.config import Settings
from behelink.errors import ProblemError, install_handlers
from behelink.pending_connect import PendingConnectStore
from behelink.ratelimit import RateLimiter
from behelink.reflector import ReflectorProtocol
from behelink.telegram_queue import TelegramQueueWaiter

_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")


class RegisterRequest(BaseModel):
    id: str
    port: int = Field(ge=1, le=65535)


class HeartbeatRequest(BaseModel):
    port: int | None = Field(default=None, ge=1, le=65535)


class RequestConnectRequest(BaseModel):
    port: int = Field(ge=1, le=65535)


class TelegramAckRequest(BaseModel):
    through_seq: int = Field(ge=1)


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
    # Cleartext path tokens for idempotent re-enable only. Hashes in the DB stay
    # authoritative for validation; losing this cache on restart costs a rotate,
    # never a security property.
    app.state.telegram_path_tokens = {}
    app.state.telegram_webhook_limiter = RateLimiter(
        settings.telegram_webhook_rate_per_minute, window_seconds=60.0
    )
    app.state.telegram_queue_waiter = TelegramQueueWaiter()

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

    def _owner_link(link_id: str, request: Request, conn: sqlite3.Connection) -> db.Link:
        token = _bearer_token(request)
        link = db.get_link(conn, link_id)
        if link is None or not tokens.verify_token(token, link.owner_token_hash):
            raise _not_found(link_id)
        return link

    def _webhook_url(link_id: str, path_token: str) -> str:
        return f"{settings.public_base_url.rstrip('/')}/v1/tg/{link_id}/{path_token}"

    def _mint_path_token(link_id: str, conn: sqlite3.Connection) -> str:
        path_token = tokens.generate_token("blw")
        db.enable_telegram_relay(
            conn, link_id, tokens.hash_token(path_token), clock.now()
        )
        app.state.telegram_path_tokens[link_id] = path_token
        return path_token

    @app.post("/v1/links/{link_id}:enableTelegramRelay")
    def enable_telegram_relay(
        link_id: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, str]:
        # With the relay switched off the ingress refuses everything, so minting
        # here would hand back a webhook_url that is dead on arrival.
        if not settings.telegram_relay_enabled:
            raise _not_found(link_id)
        link = _owner_link(link_id, request, conn)
        # Idempotent: the token is hashed at rest and cannot be recovered, so a
        # second enable must not mint a new one or it would silently break the
        # webhook URL already registered with Telegram.
        cached = app.state.telegram_path_tokens.get(link_id)
        if link.telegram_path_token_hash is not None and cached is not None:
            return {"webhook_url": _webhook_url(link_id, cached), "path_token": cached}
        path_token = _mint_path_token(link_id, conn)
        return {"webhook_url": _webhook_url(link_id, path_token), "path_token": path_token}

    @app.post("/v1/links/{link_id}:rotateWebhookPathToken")
    def rotate_webhook_path_token(
        link_id: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, str]:
        if not settings.telegram_relay_enabled:
            raise _not_found(link_id)
        _owner_link(link_id, request, conn)
        path_token = _mint_path_token(link_id, conn)
        return {"webhook_url": _webhook_url(link_id, path_token), "path_token": path_token}

    @app.post("/v1/links/{link_id}:disableTelegramRelay", status_code=204)
    def disable_telegram_relay(
        link_id: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        _owner_link(link_id, request, conn)
        db.disable_telegram_relay(conn, link_id)
        app.state.telegram_path_tokens.pop(link_id, None)
        return Response(status_code=204)

    @app.post("/v1/tg/{link_id}/{path_token}")
    async def telegram_ingress(
        link_id: str,
        path_token: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        if not settings.telegram_relay_enabled:
            raise _not_found(link_id)
        link = db.get_link(conn, link_id)
        # Unknown link, relay disabled, and bad token are one indistinguishable
        # 404 — this endpoint is unauthenticated and public, so distinguishing
        # them would let anyone enumerate link IDs.
        if (
            link is None
            or link.telegram_path_token_hash is None
            or not tokens.verify_token(path_token, link.telegram_path_token_hash)
        ):
            raise _not_found(link_id)
        limiter = app.state.telegram_webhook_limiter
        # Per link *and* per source IP: per-link alone lets one sender flood
        # across many links, per-IP alone lets many senders flood one link.
        if not limiter.allow(f"link:{link_id}") or not limiter.allow(
            f"ip:{_client_ip(request)}"
        ):
            raise ProblemError(
                429,
                "rate_limited",
                "Too Many Requests",
                "webhook rate limit exceeded",
                headers={"Retry-After": "60"},
            )
        payload = await request.body()
        if len(payload) > settings.telegram_max_payload_bytes:
            raise ProblemError(
                413, "usage", "Payload Too Large", "update exceeds configured maximum"
            )
        # Swept here rather than on a timer: behelink stays single-process and
        # testable, and the queue only grows when ingress happens — so that is
        # exactly when it needs trimming.
        db.sweep_expired(conn, cutoff=clock.now() - settings.telegram_queue_ttl_seconds)
        seq = db.enqueue_update(
            conn,
            link_id,
            payload=payload,
            secret_header=request.headers.get("X-Telegram-Bot-Api-Secret-Token"),
            received_at=clock.now(),
            max_depth=settings.telegram_queue_max_depth,
        )
        if seq is None:
            # 503 (not 200) so Telegram retries. Answering 200 here would make
            # this a silent, permanent loss of a user's command.
            raise ProblemError(
                503,
                "unavailable",
                "Service Unavailable",
                "update queue is full; retry later",
                headers={"Retry-After": "10"},
            )
        # Notify only after enqueue_update has committed, so a woken consumer
        # always finds the row it was woken for.
        app.state.telegram_queue_waiter.notify(link_id)
        return JSONResponse({"ok": True})

    @app.get("/v1/links/{link_id}/telegram/updates")
    async def telegram_updates(
        link_id: str,
        request: Request,
        wait: float = Query(default=0.0, ge=0.0),
        limit: int = Query(default=100, ge=1),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        _owner_link(link_id, request, conn)
        capped = min(limit, settings.telegram_updates_max_batch)
        rows = db.fetch_updates(conn, link_id, limit=capped)
        if not rows:
            await app.state.telegram_queue_waiter.wait(
                link_id,
                timeout=min(wait, settings.telegram_updates_wait_max_seconds),
                # Re-read the durable queue after the waiter clears its event, so
                # an enqueue racing that clear can't strand a committed update
                # until the full timeout expires.
                peek=lambda: bool(db.fetch_updates(conn, link_id, limit=1)),
            )
            rows = db.fetch_updates(conn, link_id, limit=capped)
        if not rows:
            return Response(status_code=204)
        # Rows are deliberately not deleted here — `ack` does that. Redelivery
        # before ack is expected and correct.
        return JSONResponse(
            {
                "updates": [
                    {
                        "seq": row.seq,
                        "received_at": _iso(row.received_at),
                        "secret_header": row.secret_header,
                        "payload": json.loads(row.payload),
                    }
                    for row in rows
                ]
            }
        )

    @app.post("/v1/links/{link_id}/telegram/ack", status_code=204)
    def telegram_ack(
        link_id: str,
        body: TelegramAckRequest,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        _owner_link(link_id, request, conn)
        db.ack_updates(conn, link_id, through_seq=body.through_seq)
        return Response(status_code=204)

    @app.get("/v1/links/{link_id}/telegram")
    def telegram_status(
        link_id: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, object]:
        link = _owner_link(link_id, request, conn)
        depth, oldest_seq, last_enqueue_at = db.queue_stats(conn, link_id)
        return {
            "enabled": link.telegram_path_token_hash is not None,
            "depth": depth,
            "oldest_seq": oldest_seq,
            "last_enqueue_at": (
                _iso(last_enqueue_at) if last_enqueue_at is not None else None
            ),
            "dropped_total": link.telegram_dropped_total,
        }

    return app
