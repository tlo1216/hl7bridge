"""Store invariants: UTC-only timestamps, duplicate handling, report rank rules."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from hl7bridge.store import connect, utc_iso
from hl7bridge.store.repo import (
    OrderRow,
    ReportRow,
    StudyRow,
    cancel_order,
    insert_quarantine,
    insert_study,
    quarantine_summary,
    record_message,
    upsert_order,
    upsert_report,
)

NOW = utc_iso(datetime(2026, 10, 7, 12, 0, tzinfo=UTC))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def test_utc_iso_rejects_naive():
    with pytest.raises(ValueError, match="naive"):
        utc_iso(datetime(2026, 10, 7, 12, 0))


def test_utc_iso_normalizes_to_utc():
    la = datetime(2026, 10, 7, 5, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert utc_iso(la) == "2026-10-07T12:00:00+00:00"


def test_record_message_duplicate_detection(conn):
    kwargs = {
        "hospital_id": "HOSPA",
        "control_id": "C1",
        "message_type": "ORM^O01",
        "received_at_utc": NOW,
        "raw": "MSH|...",
    }
    with conn:
        assert record_message(conn, **kwargs) == "new"
        assert record_message(conn, **kwargs) == "duplicate"
        assert record_message(conn, **kwargs) == "duplicate"
    row = conn.execute("SELECT COUNT(*) AS n, MAX(duplicate_count) AS d FROM messages").fetchone()
    assert (row["n"], row["d"]) == (1, 2)


def _order(**overrides) -> OrderRow:
    base = {
        "hospital_id": "HOSPA",
        "accession": "ACC1",
        "placer_order_id": "P1",
        "procedure_code": "71250",
        "procedure_description": "CT CHEST WO CONTRAST",
        "code_system": "local",
        "modality": "CT",
        "patient_mrn": "M1",
        "ordered_at_utc": NOW,
        "status": "new",
    }
    base.update(overrides)
    return OrderRow(**base)


def test_order_upsert_and_cancel(conn):
    with conn:
        assert upsert_order(conn, _order()) == "inserted"
        assert upsert_order(conn, _order(modality="MR")) == "updated"
        assert cancel_order(conn, hospital_id="HOSPA", accession="ACC1") == "cancelled"
        assert cancel_order(conn, hospital_id="HOSPA", accession="NOPE") == "not_found"
    row = conn.execute("SELECT modality, status FROM orders").fetchone()
    assert (row["modality"], row["status"]) == ("MR", "cancelled")


def _report(status, control_id="R1", text="No acute findings.") -> ReportRow:
    return ReportRow(
        hospital_id="HOSPA",
        accession="ACC1",
        control_id=control_id,
        status=status,
        report_text=text,
        patient_mrn="M1",
        result_at_utc=NOW,
        received_at_utc=NOW,
    )


def test_report_rank_progression_and_regression(conn):
    with conn:
        assert upsert_report(conn, _report("P")) == "inserted"
        assert upsert_report(conn, _report("F", "R2")) == "updated"
        assert upsert_report(conn, _report("C", "R3", "Corrected.")) == "updated"
        # A stale preliminary after a correction must not clobber it.
        assert upsert_report(conn, _report("P", "R4", "Stale.")) == "ignored_regression"
    row = conn.execute("SELECT status, report_text, control_id FROM reports").fetchone()
    assert (row["status"], row["report_text"], row["control_id"]) == ("C", "Corrected.", "R3")


def test_study_nullable_accession(conn):
    s = StudyRow(
        study_uid="1.2.3",
        hospital_id="HOSPA",
        accession=None,
        modality="CT",
        patient_mrn="M1",
        study_at_utc=NOW,
        source_file="x.dcm",
    )
    with conn:
        assert insert_study(conn, s) == "inserted"
        assert insert_study(conn, s) == "replaced"
    assert conn.execute("SELECT accession FROM studies").fetchone()["accession"] is None


def test_quarantine_summary(conn):
    with conn:
        for reason in ("parse_error", "parse_error", "unknown_hospital"):
            insert_quarantine(
                conn,
                hospital_id=None,
                reason=reason,
                detail="",
                raw=b"junk",
                ack_code="AR",
                received_at_utc=NOW,
            )
    assert quarantine_summary(conn) == {"parse_error": 2, "unknown_hospital": 1}


def test_ack_code_check_constraint(conn):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "INSERT INTO messages (hospital_id, control_id, message_type, received_at_utc,"
            " raw, ack_code) VALUES ('H', 'C', 'T', ?, 'r', 'XX')",
            (NOW,),
        )
