"""Reconciliation: every category detected, clean data produces zero flags."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.reconcile import reconcile
from hl7bridge.store import connect, utc_iso
from hl7bridge.store.repo import OrderRow, StudyRow, insert_study, upsert_order

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "hospitals"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
MORNING = utc_iso(datetime(2026, 10, 7, 15, 0, tzinfo=UTC))  # well past all thresholds
JUST_NOW = utc_iso(datetime(2026, 10, 8, 11, 45, tzinfo=UTC))  # inside grace/thresholds


@pytest.fixture
def registry():
    return HospitalRegistry.load_dir(CONFIG_DIR)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "r.db")
    yield c
    c.close()


def order(acc, *, modality="CT", mrn="M1", status="new", at=MORNING, hosp="HOSPA"):
    return OrderRow(
        hospital_id=hosp,
        accession=acc,
        placer_order_id=None,
        procedure_code="71250",
        procedure_description="CT CHEST",
        code_system="local",
        modality=modality,
        priority=None,
        patient_mrn=mrn,
        ordered_at_utc=at,
        status=status,
    )


def study(acc, *, uid, modality="CT", mrn="M1", at=MORNING, hosp="HOSPA"):
    return StudyRow(
        study_uid=uid,
        hospital_id=hosp,
        accession=acc,
        modality=modality,
        patient_mrn=mrn,
        study_at_utc=at,
        source_file=None,
    )


def run(conn, registry):
    return reconcile(conn, registry, NOW)


def test_clean_pair_matches_with_zero_findings(conn, registry):
    with conn:
        upsert_order(conn, order("ACC1"))
        insert_study(conn, study("ACC1", uid="1.1"))
    r = run(conn, registry)["HOSPA"]
    assert (r.matched, r.findings) == (1, [])


def test_accession_match_normalizes_whitespace_and_case_only(conn, registry):
    with conn:
        upsert_order(conn, order("acc1 "))
        insert_study(conn, study(" ACC1", uid="1.1"))
        upsert_order(conn, order("ACC2"))
        insert_study(conn, study("ACC-2", uid="1.2"))  # fuzzier than case: no match
    r = run(conn, registry)["HOSPA"]
    assert r.matched == 1
    cats = sorted(f.category for f in r.findings)
    assert cats == ["IMAGE_WITHOUT_ORDER", "ORDER_WITHOUT_IMAGE"]


def test_order_without_image_respects_modality_threshold(conn, registry):
    # HOSPA: default 4h, US overridden to 2h.
    three_hours_ago = utc_iso(datetime(2026, 10, 8, 9, 0, tzinfo=UTC))
    with conn:
        upsert_order(conn, order("CT3H", modality="CT", at=three_hours_ago))
        upsert_order(conn, order("US3H", modality="US", at=three_hours_ago))
    r = run(conn, registry)["HOSPA"]
    flagged = {f.accession for f in r.by_category("ORDER_WITHOUT_IMAGE")}
    assert flagged == {"US3H"}
    assert r.pending_orders == 1  # the CT order is still inside its window


def test_image_without_order_grace_period(conn, registry):
    with conn:
        insert_study(conn, study("OLD", uid="2.1", at=MORNING))
        insert_study(conn, study("NEW", uid="2.2", at=JUST_NOW))
    r = run(conn, registry)["HOSPA"]
    assert {f.accession for f in r.by_category("IMAGE_WITHOUT_ORDER")} == {"OLD"}
    assert r.pending_studies == 1


def test_study_with_empty_accession_is_flagged(conn, registry):
    with conn:
        insert_study(conn, study(None, uid="3.1"))
    r = run(conn, registry)["HOSPA"]
    findings = r.by_category("IMAGE_WITHOUT_ORDER")
    assert len(findings) == 1
    assert "empty AccessionNumber" in findings[0].detail


def test_mismatch_patient_and_modality(conn, registry):
    with conn:
        upsert_order(conn, order("ACCP", mrn="M1"))
        insert_study(conn, study("ACCP", uid="4.1", mrn="M2"))
        upsert_order(conn, order("ACCM", modality="CT"))
        insert_study(conn, study("ACCM", uid="4.2", modality="US"))
    r = run(conn, registry)["HOSPA"]
    assert {f.category for f in r.findings} == {"MISMATCH_PATIENT", "MISMATCH_MODALITY"}
    assert r.matched == 0


def test_cancelled_order_without_study_not_flagged(conn, registry):
    with conn:
        upsert_order(conn, order("ACCC", status="cancelled"))
    r = run(conn, registry)["HOSPA"]
    assert r.findings == []
    assert r.matched == 0


def test_cancelled_order_with_study_is_flagged(conn, registry):
    with conn:
        upsert_order(conn, order("ACCC", status="cancelled"))
        insert_study(conn, study("ACCC", uid="5.1"))
    r = run(conn, registry)["HOSPA"]
    assert [f.category for f in r.findings] == ["CANCELLED_ORDER_WITH_IMAGE"]


def test_multiple_studies_per_accession_single_match(conn, registry):
    with conn:
        upsert_order(conn, order("ACCX"))
        insert_study(conn, study("ACCX", uid="6.1"))
        insert_study(conn, study("ACCX", uid="6.2"))
    r = run(conn, registry)["HOSPA"]
    assert (r.matched, r.findings) == (1, [])


def test_naive_now_rejected(conn, registry):
    with pytest.raises(ValueError, match="aware"):
        reconcile(conn, registry, datetime(2026, 10, 8, 12, 0))
