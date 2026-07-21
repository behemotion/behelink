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
