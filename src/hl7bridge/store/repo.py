"""Typed repository functions over the SQLite store.

Each function does one write inside the caller's transaction (the ingest
pipeline wraps one message in one transaction) and reports what it did.
Nothing here swallows errors; constraint violations propagate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

# Report status rank: a corrected report supersedes a final, which supersedes
# a preliminary. A lower-rank arrival after a higher rank is recorded as a
# regression and does not overwrite (spec: corrected reports update, never
# duplicate; stale preliminaries never clobber finals).
_REPORT_RANK = {"P": 0, "F": 1, "C": 2}


@dataclass(frozen=True, slots=True)
class PatientRow:
    hospital_id: str
    mrn: str
    family_name: str
    given_name: str
    birth_date: str | None
    sex: str | None


@dataclass(frozen=True, slots=True)
class OrderRow:
    hospital_id: str
    accession: str
    placer_order_id: str | None
    procedure_code: str | None
    procedure_description: str | None
    code_system: str | None
    modality: str | None
    priority: str | None
    patient_mrn: str | None
    ordered_at_utc: str
    status: Literal["new", "cancelled"]


@dataclass(frozen=True, slots=True)
class ReportRow:
    hospital_id: str
    accession: str
    control_id: str
    status: Literal["P", "F", "C"]
    report_text: str
    patient_mrn: str | None
    result_at_utc: str | None
    received_at_utc: str


@dataclass(frozen=True, slots=True)
class ReportEventRow:
    hospital_id: str
    accession: str
    control_id: str
    status: Literal["P", "F", "C"]
    observed_at_utc: str | None
    result_at_utc: str | None
    received_at_utc: str


@dataclass(frozen=True, slots=True)
class StudyRow:
    study_uid: str
    hospital_id: str
    accession: str | None
    modality: str
    patient_mrn: str | None
    study_at_utc: str
    source_file: str | None


@dataclass(frozen=True, slots=True)
class AckInRow:
    hospital_id: str
    ack_control_id: str
    msa_code: str
    msa_control_id: str
    reason: str | None
    received_at_utc: str


def record_message(
    conn: sqlite3.Connection,
    *,
    hospital_id: str,
    control_id: str,
    message_type: str,
    received_at_utc: str,
    raw: str,
) -> Literal["new", "duplicate"]:
    """Record an accepted message; re-deliveries bump duplicate_count."""
    cur = conn.execute(
        """
        INSERT INTO messages (hospital_id, control_id, message_type, received_at_utc, raw, ack_code)
        VALUES (?, ?, ?, ?, ?, 'AA')
        ON CONFLICT (hospital_id, control_id)
        DO UPDATE SET duplicate_count = duplicate_count + 1
        """,
        (hospital_id, control_id, message_type, received_at_utc, raw),
    )
    row = conn.execute(
        "SELECT duplicate_count FROM messages WHERE hospital_id = ? AND control_id = ?",
        (hospital_id, control_id),
    ).fetchone()
    assert cur.rowcount == 1 and row is not None
    return "new" if row["duplicate_count"] == 0 else "duplicate"


def upsert_patient(conn: sqlite3.Connection, p: PatientRow) -> None:
    conn.execute(
        """
        INSERT INTO patients (hospital_id, mrn, family_name, given_name, birth_date, sex)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (hospital_id, mrn) DO UPDATE SET
            family_name = excluded.family_name,
            given_name = excluded.given_name,
            birth_date = excluded.birth_date,
            sex = excluded.sex
        """,
        (p.hospital_id, p.mrn, p.family_name, p.given_name, p.birth_date, p.sex),
    )


def record_merge(
    conn: sqlite3.Connection,
    *,
    hospital_id: str,
    surviving_mrn: str,
    merged_mrn: str,
    merged_at_utc: str,
) -> None:
    conn.execute(
        "INSERT INTO merges (hospital_id, surviving_mrn, merged_mrn, merged_at_utc)"
        " VALUES (?, ?, ?, ?)",
        (hospital_id, surviving_mrn, merged_mrn, merged_at_utc),
    )
    conn.execute(
        "UPDATE patients SET merged_into_mrn = ? WHERE hospital_id = ? AND mrn = ?",
        (surviving_mrn, hospital_id, merged_mrn),
    )


def upsert_order(conn: sqlite3.Connection, o: OrderRow) -> Literal["inserted", "updated"]:
    existing = conn.execute(
        "SELECT 1 FROM orders WHERE hospital_id = ? AND accession = ?",
        (o.hospital_id, o.accession),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO orders (hospital_id, accession, placer_order_id, procedure_code,
                            procedure_description, code_system, modality, priority,
                            patient_mrn, ordered_at_utc, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (hospital_id, accession) DO UPDATE SET
            placer_order_id = excluded.placer_order_id,
            procedure_code = excluded.procedure_code,
            procedure_description = excluded.procedure_description,
            code_system = excluded.code_system,
            modality = excluded.modality,
            priority = excluded.priority,
            patient_mrn = excluded.patient_mrn,
            ordered_at_utc = excluded.ordered_at_utc,
            status = excluded.status
        """,
        (
            o.hospital_id,
            o.accession,
            o.placer_order_id,
            o.procedure_code,
            o.procedure_description,
            o.code_system,
            o.modality,
            o.priority,
            o.patient_mrn,
            o.ordered_at_utc,
            o.status,
        ),
    )
    return "updated" if existing else "inserted"


def cancel_order(
    conn: sqlite3.Connection, *, hospital_id: str, accession: str
) -> Literal["cancelled", "not_found"]:
    cur = conn.execute(
        "UPDATE orders SET status = 'cancelled' WHERE hospital_id = ? AND accession = ?",
        (hospital_id, accession),
    )
    return "cancelled" if cur.rowcount == 1 else "not_found"


def upsert_report(
    conn: sqlite3.Connection, r: ReportRow
) -> Literal["inserted", "updated", "ignored_regression"]:
    existing = conn.execute(
        "SELECT status FROM reports WHERE hospital_id = ? AND accession = ?",
        (r.hospital_id, r.accession),
    ).fetchone()
    if existing is not None and _REPORT_RANK[r.status] < _REPORT_RANK[existing["status"]]:
        return "ignored_regression"
    conn.execute(
        """
        INSERT INTO reports (hospital_id, accession, control_id, status, report_text,
                             patient_mrn, result_at_utc, received_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (hospital_id, accession) DO UPDATE SET
            control_id = excluded.control_id,
            status = excluded.status,
            report_text = excluded.report_text,
            patient_mrn = excluded.patient_mrn,
            result_at_utc = excluded.result_at_utc,
            received_at_utc = excluded.received_at_utc
        """,
        (
            r.hospital_id,
            r.accession,
            r.control_id,
            r.status,
            r.report_text,
            r.patient_mrn,
            r.result_at_utc,
            r.received_at_utc,
        ),
    )
    return "updated" if existing else "inserted"


def insert_study(conn: sqlite3.Connection, s: StudyRow) -> Literal["inserted", "replaced"]:
    existing = conn.execute("SELECT 1 FROM studies WHERE study_uid = ?", (s.study_uid,)).fetchone()
    conn.execute(
        """
        INSERT OR REPLACE INTO studies (study_uid, hospital_id, accession, modality,
                                        patient_mrn, study_at_utc, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            s.study_uid,
            s.hospital_id,
            s.accession,
            s.modality,
            s.patient_mrn,
            s.study_at_utc,
            s.source_file,
        ),
    )
    return "replaced" if existing else "inserted"


def insert_report_event(conn: sqlite3.Connection, e: ReportEventRow) -> None:
    """Append-only: every accepted ORU is one event, never overwritten."""
    conn.execute(
        """
        INSERT INTO report_events (hospital_id, accession, control_id, status,
                                   observed_at_utc, result_at_utc, received_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            e.hospital_id,
            e.accession,
            e.control_id,
            e.status,
            e.observed_at_utc,
            e.result_at_utc,
            e.received_at_utc,
        ),
    )


def record_outbound_report(
    conn: sqlite3.Connection,
    *,
    hospital_id: str,
    control_id: str,
    accession: str,
    sent_at_utc: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO outbound_reports (hospital_id, control_id, accession, sent_at_utc)"
        " VALUES (?, ?, ?, ?)",
        (hospital_id, control_id, accession, sent_at_utc),
    )


def insert_ack_in(conn: sqlite3.Connection, a: AckInRow) -> None:
    conn.execute(
        """
        INSERT INTO acks_in (hospital_id, ack_control_id, msa_code, msa_control_id,
                             reason, received_at_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            a.hospital_id,
            a.ack_control_id,
            a.msa_code,
            a.msa_control_id,
            a.reason,
            a.received_at_utc,
        ),
    )


def insert_quarantine(
    conn: sqlite3.Connection,
    *,
    hospital_id: str | None,
    reason: str,
    detail: str,
    raw: bytes,
    ack_code: Literal["AE", "AR"],
    received_at_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO quarantine (hospital_id, reason, detail, raw, ack_code, received_at_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (hospital_id, reason, detail, raw, ack_code, received_at_utc),
    )


def quarantine_summary(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT reason, COUNT(*) AS n FROM quarantine GROUP BY reason ORDER BY reason"
    ).fetchall()
    return {row["reason"]: row["n"] for row in rows}


def message_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT message_type, COUNT(*) AS n FROM messages GROUP BY message_type"
        " ORDER BY message_type"
    ).fetchall()
    return {row["message_type"]: row["n"] for row in rows}
