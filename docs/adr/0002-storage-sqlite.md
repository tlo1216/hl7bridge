# ADR-0002: SQLite via the standard library

Date: 2026-10-09
Status: accepted

## Context

The demo needs durable storage for messages, patients, orders, studies,
reports, ACKs, quarantine, and merges. One writer (the MLLP pipeline), a few
readers (report commands). Volume is thousands of rows, not millions.

## Decision

SQLite through stdlib `sqlite3`, no ORM. Schema in one `schema.sql` with a
`schema_version` row that the code refuses to run against if it mismatches
(no silent auto-migration). WAL mode. All timestamps stored as ISO 8601 UTC
strings; `utc_iso()` rejects naive datetimes at the door, and
`parse_utc_iso()` rejects naive values coming back out, so the
naive-vs-aware comparison bug class cannot enter the store.

## Why not an ORM or Postgres

- An ORM hides the exact upsert semantics we are testing (report rank
  upgrades, duplicate-message counting). The repository functions return
  what they did (`inserted` / `updated` / `ignored_regression`), which the
  tests assert directly.
- Postgres buys concurrency we do not have and costs a service dependency
  in every test. The repository layer is thin enough that a later move is a
  schema-and-SQL port, not a redesign.

## Consequences

- One process owns writes; the ingest pipeline wraps one message in one
  transaction, so a validation failure rolls back all of that message's
  writes (tested).
- Report-status regressions (P after C) are recorded as refusals, never
  silent overwrites.
