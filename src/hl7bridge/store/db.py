"""SQLite connection and schema management."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from ..errors import StoreError

# v2: orders.priority, report_events table. No migration path by design for
# the demo: a mismatched database fails loudly and is recreated.
SCHEMA_VERSION = 2


def utc_iso(dt: datetime) -> str:
    """Render an aware datetime as an ISO 8601 UTC string for storage.

    Naive datetimes are rejected: comparing naive and aware timestamps is the
    bug class the aware-timestamps-only rule exists to prevent.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("refusing to store a naive datetime")
    return dt.astimezone(UTC).isoformat()


def parse_utc_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("stored timestamp is naive; storage invariant violated")
    return dt


def connect(path: str | Path, *, create: bool = True, wal: bool = True) -> sqlite3.Connection:
    """Open the store. ``create`` False opens an existing file only and
    never makes one (the report commands use that). A file that is not an
    hl7bridge database, holds another program's tables, or holds another
    schema version raises StoreError with the path and what to do. ``wal``
    False keeps SQLite's default rollback journal, which leaves no side
    files behind once the connection closes (the demo uses that)."""
    target = str(path)
    open_as, uri = target, False
    if target != ":memory:":
        file = Path(target)
        if not create:
            if not file.exists():
                raise StoreError(
                    f"no database at {target}.",
                    hint="Run hl7bridge listen or hl7bridge load first, or pass --db"
                    " with the database file.",
                )
            if not file.is_file():
                raise StoreError(
                    f"{target} is not a file.", hint="Pass --db with the database file."
                )
            # mode=rw: SQLite opens the file but never creates it.
            open_as, uri = f"{file.resolve().as_uri()}?mode=rw", True
        parent = file.parent
        if not parent.is_dir():
            raise StoreError(
                f"cannot create database {target}: folder {parent} does not exist",
                hint="Create the folder first, or pass --db with another path.",
            )
    try:
        conn = sqlite3.connect(open_as, uri=uri)
    except sqlite3.Error as exc:
        raise StoreError(
            f"cannot open database {target} ({type(exc).__name__}).",
            hint="Pass --db with the path of a database file.",
        ) from None
    try:
        conn.row_factory = sqlite3.Row
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(conn, target)
    except StoreError:
        conn.close()
        raise
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise StoreError(
            f"{target} is not an hl7bridge database ({type(exc).__name__}).",
            hint="Pass --db with another path, or delete the file if it is a demo database.",
        ) from None
    return conn


def schema_mismatch_message(target: str, found: object) -> str:
    return (
        f"database {target} has schema version {found}; this hl7bridge needs version "
        f"{SCHEMA_VERSION}. There is no migration."
    )


def _init_schema(conn: sqlite3.Connection, target: str = "") -> None:
    schema_sql = (files("hl7bridge.store") / "schema.sql").read_text(encoding="utf-8")
    with conn:
        row = None
        if _has_table(conn, "schema_version"):
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        elif conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone():
            # Tables, but not ours: never add hl7bridge tables to another
            # program's database.
            raise StoreError(
                f"{target or '(memory)'} is a SQLite database that hl7bridge did not create.",
                hint="Pass --db with another path.",
            )
        if row is not None and row["version"] != SCHEMA_VERSION:
            raise StoreError(
                schema_mismatch_message(target or "(memory)", row["version"]),
                hint="Demo databases can be deleted and made again.",
            )
        conn.executescript(schema_sql)
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return found is not None


def inspect_db(path: Path) -> tuple[str, int | None]:
    """Read-only look at a database for ``doctor``. Never creates or changes
    the file. Returns (state, schema version) where state is 'missing',
    'ok', 'old_schema', 'no_schema' or 'not_a_database'."""
    if not path.exists():
        return "missing", None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return "not_a_database", None
    try:
        if not _has_table(conn, "schema_version"):
            return "no_schema", None
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.DatabaseError:
        return "not_a_database", None
    finally:
        conn.close()
    if row is None:
        return "no_schema", None
    version = int(row[0])
    return ("ok" if version == SCHEMA_VERSION else "old_schema"), version
