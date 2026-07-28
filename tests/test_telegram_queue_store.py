import sqlite3

import pytest

from behelink import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "q.db"))
    c.execute(
        "INSERT INTO links (id, port, ip, owner_token_hash, resolve_token_hash,"
        " created_at, last_seen) VALUES ('acme', 1, '1.2.3.4', 'oh', 'rh', 1.0, 2.0)"
    )
    c.commit()
    return c


def test_connect_adds_telegram_columns_to_existing_links_table(tmp_path):
    path = str(tmp_path / "legacy.db")
    # Simulate a pre-existing database created before this feature.
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE links (id TEXT PRIMARY KEY, port INTEGER NOT NULL,"
        " ip TEXT NOT NULL, owner_token_hash TEXT NOT NULL,"
        " resolve_token_hash TEXT NOT NULL, created_at REAL NOT NULL,"
        " last_seen REAL NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO links VALUES ('acme', 1, '1.2.3.4', 'oh', 'rh', 1.0, 2.0)"
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(links)")}
    assert "telegram_path_token_hash" in columns
    assert "telegram_relay_enabled_at" in columns
    assert "telegram_dropped_total" in columns

    # The dataclass must still hydrate from a full row.
    link = db.get_link(conn, "acme")
    assert link is not None
    assert link.telegram_path_token_hash is None
    assert link.telegram_dropped_total == 0


def test_connect_creates_telegram_updates_table(tmp_path):
    conn = db.connect(str(tmp_path / "fresh.db"))
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(telegram_updates)")}
    assert columns == {"seq", "link_id", "payload", "secret_header", "received_at"}


def test_enqueue_returns_increasing_seq_and_fetch_is_fifo(conn):
    first = db.enqueue_update(conn, "acme", b'{"update_id":1}', "sek", 10.0, max_depth=10)
    second = db.enqueue_update(conn, "acme", b'{"update_id":2}', "sek", 11.0, max_depth=10)
    assert second > first

    rows = db.fetch_updates(conn, "acme", limit=10)
    assert [r.seq for r in rows] == [first, second]
    assert rows[0].payload == b'{"update_id":1}'
    assert rows[0].secret_header == "sek"


def test_fetch_does_not_delete_so_redelivery_happens_before_ack(conn):
    db.enqueue_update(conn, "acme", b"a", None, 10.0, max_depth=10)
    assert len(db.fetch_updates(conn, "acme", limit=10)) == 1
    assert len(db.fetch_updates(conn, "acme", limit=10)) == 1


def test_ack_deletes_through_seq_and_no_further(conn):
    first = db.enqueue_update(conn, "acme", b"a", None, 10.0, max_depth=10)
    second = db.enqueue_update(conn, "acme", b"b", None, 11.0, max_depth=10)

    assert db.ack_updates(conn, "acme", through_seq=first) == 1
    remaining = db.fetch_updates(conn, "acme", limit=10)
    assert [r.seq for r in remaining] == [second]


def test_enqueue_returns_none_when_queue_is_full(conn):
    for i in range(3):
        assert db.enqueue_update(conn, "acme", b"x", None, float(i), max_depth=3) is not None
    assert db.enqueue_update(conn, "acme", b"x", None, 9.0, max_depth=3) is None


def test_queue_is_durable_across_reconnect(conn, tmp_path):
    # This is the property that distinguishes this queue from PendingConnectStore.
    path = str(tmp_path / "q.db")
    seq = db.enqueue_update(conn, "acme", b"survives", None, 10.0, max_depth=10)
    conn.close()

    reopened = db.connect(path)
    rows = db.fetch_updates(reopened, "acme", limit=10)
    assert [r.seq for r in rows] == [seq]
    assert rows[0].payload == b"survives"


def test_sweep_deletes_expired_and_counts_them_as_dropped(conn):
    db.enqueue_update(conn, "acme", b"old", None, 100.0, max_depth=10)
    db.enqueue_update(conn, "acme", b"new", None, 500.0, max_depth=10)

    assert db.sweep_expired(conn, cutoff=200.0) == 1
    assert [r.payload for r in db.fetch_updates(conn, "acme", limit=10)] == [b"new"]
    link = db.get_link(conn, "acme")
    assert link.telegram_dropped_total == 1


def test_queue_stats_reports_depth_oldest_and_last_enqueue(conn):
    assert db.queue_stats(conn, "acme") == (0, None, None)
    first = db.enqueue_update(conn, "acme", b"a", None, 10.0, max_depth=10)
    db.enqueue_update(conn, "acme", b"b", None, 20.0, max_depth=10)
    assert db.queue_stats(conn, "acme") == (2, first, 20.0)
