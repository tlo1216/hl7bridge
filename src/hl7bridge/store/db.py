"""SQLite connection and schema management."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 1


def utc_iso(dt: datetime) -> str:
    """Render an aware datetime as an ISO 8601 UTC string for storage.

    Naive datetimes are rejected: comparing naive and aware timestamps is the
    bug class engineering rule 4 exists to prevent.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("refusing to store a naive datetime")
    return dt.astimezone(UTC).isoformat()


def parse_utc_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"stored timestamp {value!r} is naive; storage invariant violated")
    return dt


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    schema_sql = (files("hl7bridge.store") / "schema.sql").read_text(encoding="utf-8")
    with conn:
        conn.executescript(schema_sql)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {row['version']} != code version {SCHEMA_VERSION}; "
                "no migration path defined, recreate the demo database"
            )
