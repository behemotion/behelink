"""SQLite storage: `links` plus the durable Telegram update queue,
connection-per-request."""

import sqlite3
from dataclasses import dataclass

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


_TELEGRAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_updates (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    secret_header TEXT,
    received_at REAL NOT NULL
)
"""

_TELEGRAM_INDEXES = (
    "CREATE INDEX IF NOT EXISTS telegram_updates_link_seq"
    " ON telegram_updates (link_id, seq)",
    "CREATE INDEX IF NOT EXISTS telegram_updates_received_at"
    " ON telegram_updates (received_at)",
)

# Added to `links` after the original schema shipped. CREATE TABLE IF NOT EXISTS
# will not add these to a database that already exists, so they are applied by
# explicit ALTER TABLE on every connect.
_LINK_ADDED_COLUMNS = {
    "telegram_path_token_hash": "TEXT",
    "telegram_relay_enabled_at": "REAL",
    "telegram_dropped_total": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate_links(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(links)")}
    for name, decl in _LINK_ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE links ADD COLUMN {name} {decl}")


def connect(path: str) -> sqlite3.Connection:
    # check_same_thread=False: async routes run on the event loop thread while the
    # sync `get_conn` dependency that creates this connection runs in FastAPI's
    # threadpool — the connection is still single-request-scoped, never shared
    # across concurrent requests.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    conn.execute(_TELEGRAM_SCHEMA)
    for statement in _TELEGRAM_INDEXES:
        conn.execute(statement)
    _migrate_links(conn)
    conn.commit()
    return conn


@dataclass
class Link:
    id: str
    port: int
    ip: str
    owner_token_hash: str
    resolve_token_hash: str
    created_at: float
    last_seen: float
    # Defaulted so every existing Link(...) construction keeps working. `get_link`
    # does Link(**dict(row)) over SELECT *, so these must exist as fields the
    # moment the ALTER TABLE above adds the columns.
    telegram_path_token_hash: str | None = None
    telegram_relay_enabled_at: float | None = None
    telegram_dropped_total: int = 0


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


def find_link_by_token_hash(conn: sqlite3.Connection, token_hash: str) -> Link | None:
    row = conn.execute(
        "SELECT * FROM links WHERE owner_token_hash = ? OR resolve_token_hash = ?",
        (token_hash, token_hash),
    ).fetchone()
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
