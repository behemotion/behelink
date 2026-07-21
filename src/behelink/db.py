"""SQLite storage: single `links` table, connection-per-request."""

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


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
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
