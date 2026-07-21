"""SQLite storage: single `links` table, connection-per-request."""

import sqlite3

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
