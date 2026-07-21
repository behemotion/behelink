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
