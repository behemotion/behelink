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


@dataclass
class TelegramUpdate:
    seq: int
    link_id: str
    payload: bytes
    secret_header: str | None
    received_at: float


def enqueue_update(
    conn: sqlite3.Connection,
    link_id: str,
    payload: bytes,
    secret_header: str | None,
    received_at: float,
    max_depth: int,
) -> int | None:
    """Commit one update. Returns its seq, or None when the queue is full.

    The depth check is advisory: two concurrent ingress requests can both pass
    it and overshoot max_depth by the number of in-flight requests. That is
    bounded by the ingress rate limiter and costs a few extra rows, whereas
    holding a write lock across the check would serialize every delivery.
    """
    (depth,) = conn.execute(
        "SELECT COUNT(*) FROM telegram_updates WHERE link_id = ?", (link_id,)
    ).fetchone()
    if depth >= max_depth:
        return None
    cursor = conn.execute(
        "INSERT INTO telegram_updates (link_id, payload, secret_header, received_at)"
        " VALUES (?, ?, ?, ?)",
        (link_id, payload, secret_header, received_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


def fetch_updates(
    conn: sqlite3.Connection, link_id: str, limit: int
) -> list[TelegramUpdate]:
    rows = conn.execute(
        "SELECT seq, link_id, payload, secret_header, received_at FROM telegram_updates"
        " WHERE link_id = ? ORDER BY seq LIMIT ?",
        (link_id, limit),
    ).fetchall()
    return [TelegramUpdate(**dict(row)) for row in rows]


def ack_updates(conn: sqlite3.Connection, link_id: str, through_seq: int) -> int:
    cursor = conn.execute(
        "DELETE FROM telegram_updates WHERE link_id = ? AND seq <= ?",
        (link_id, through_seq),
    )
    conn.commit()
    return cursor.rowcount


def queue_stats(
    conn: sqlite3.Connection, link_id: str
) -> tuple[int, int | None, float | None]:
    row = conn.execute(
        "SELECT COUNT(*) AS depth, MIN(seq) AS oldest, MAX(received_at) AS last"
        " FROM telegram_updates WHERE link_id = ?",
        (link_id,),
    ).fetchone()
    return int(row["depth"]), row["oldest"], row["last"]


def sweep_expired(conn: sqlite3.Connection, cutoff: float) -> int:
    """Delete rows older than cutoff, crediting each link's dropped counter."""
    rows = conn.execute(
        "SELECT link_id, COUNT(*) AS n FROM telegram_updates"
        " WHERE received_at < ? GROUP BY link_id",
        (cutoff,),
    ).fetchall()
    total = 0
    for row in rows:
        conn.execute(
            "UPDATE links SET telegram_dropped_total = telegram_dropped_total + ?"
            " WHERE id = ?",
            (row["n"], row["link_id"]),
        )
        total += int(row["n"])
    conn.execute("DELETE FROM telegram_updates WHERE received_at < ?", (cutoff,))
    conn.commit()
    return total


def enable_telegram_relay(
    conn: sqlite3.Connection, link_id: str, path_token_hash: str, enabled_at: float
) -> None:
    conn.execute(
        "UPDATE links SET telegram_path_token_hash = ?, telegram_relay_enabled_at = ?"
        " WHERE id = ?",
        (path_token_hash, enabled_at, link_id),
    )
    conn.commit()


def disable_telegram_relay(conn: sqlite3.Connection, link_id: str) -> None:
    conn.execute(
        "UPDATE links SET telegram_path_token_hash = NULL,"
        " telegram_relay_enabled_at = NULL WHERE id = ?",
        (link_id,),
    )
    conn.execute("DELETE FROM telegram_updates WHERE link_id = ?", (link_id,))
    conn.commit()
