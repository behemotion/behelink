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
