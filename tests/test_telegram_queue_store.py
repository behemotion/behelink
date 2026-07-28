import sqlite3

from behelink import db


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
