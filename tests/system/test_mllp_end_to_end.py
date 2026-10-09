"""End-to-end: generated corpus over real MLLP sockets, asserted against
the ground-truth manifest. This is the system test engineering rule 6 asks for."""

import asyncio
from datetime import datetime

import pytest

from hl7bridge.acks import track_acks
from hl7bridge.ingest import IngestPipeline, send_messages, serve
from hl7bridge.ingest.parser import get_field, parse_hl7
from hl7bridge.reconcile import reconcile
from hl7bridge.store import connect, quarantine_summary

from .corpus import INGEST_NOW, build_corpus


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    out, registry, manifest = build_corpus(tmp)
    conn = connect(tmp / "e2e.db")
    pipeline = IngestPipeline(registry, conn, clock=lambda: INGEST_NOW)

    payload_paths = sorted((out / "messages").rglob("*.hl7"))
    payloads = [p.read_bytes() for p in payload_paths]

    async def run() -> list[bytes]:
        server = await serve("127.0.0.1", 0, pipeline.handle_frame, pipeline.handle_junk)
        port = server.sockets[0].getsockname()[1]
        try:
            return await send_messages("127.0.0.1", port, payloads)
        finally:
            server.close()
            await server.wait_closed()

    acks = asyncio.run(run())
    from hl7bridge.ingest.loaders import load_outbound, load_studies

    load_studies(conn, registry, out / "dicom")
    load_outbound(conn, out / "outbound_reports.jsonl")
    yield conn, registry, manifest, acks
    conn.close()


def test_every_message_got_exactly_one_ack(flow):
    conn, _registry, manifest, acks = flow
    codes = [get_field(parse_hl7(a.decode()), "MSA-1") for a in acks]
    assert all(code in ("AA", "AE", "AR") for code in codes)
    assert codes.count("AA") == manifest.expected["messages_aa_total"]
    aa_rows = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert aa_rows == manifest.expected["messages_aa_total"]


def test_quarantine_matches_manifest_exactly(flow):
    conn, _registry, manifest, _acks = flow
    assert quarantine_summary(conn) == manifest.expected["quarantine"]


def test_reconciliation_finds_all_seeded_defects_with_zero_false_flags(flow):
    conn, registry, manifest, _acks = flow
    now = datetime.fromisoformat(manifest.report_now_utc)
    reports = reconcile(conn, registry, now)
    for hosp, expected in manifest.expected["reconcile"].items():
        r = reports[hosp]
        for category in (
            "ORDER_WITHOUT_IMAGE",
            "IMAGE_WITHOUT_ORDER",
            "MISMATCH_PATIENT",
            "MISMATCH_MODALITY",
        ):
            got = sorted(f.accession or "" for f in r.by_category(category))
            want = sorted(expected[category])
            assert got == want, f"{hosp} {category}: got {got}, want {want}"
        assert r.by_category("CANCELLED_ORDER_WITH_IMAGE") == []
        assert r.matched == expected["matched"], hosp
        assert r.pending_orders == 0 and r.pending_studies == 0, hosp


def test_ack_tracking_matches_manifest(flow):
    conn, registry, manifest, _acks = flow
    now = datetime.fromisoformat(manifest.report_now_utc)
    report = track_acks(conn, registry, now)
    for hosp, expected in manifest.expected["acks"].items():
        states = report.by_state(hosp)
        for state in ("DELIVERED", "ERRORED", "REJECTED", "NO_ACK"):
            assert len(states[state]) == expected[state], f"{hosp} {state}"
        assert len(states["PENDING"]) == 0, hosp
        dup_total = sum(o.duplicate_acks for o in report.outcomes if o.hospital_id == hosp)
        assert dup_total == expected["duplicate_acks"], hosp
    unknown_by_hosp = {}
    for a in report.unknown_acks:
        unknown_by_hosp[a["hospital_id"]] = unknown_by_hosp.get(a["hospital_id"], 0) + 1
    for hosp, expected in manifest.expected["acks"].items():
        assert unknown_by_hosp.get(hosp, 0) == expected["unknown_control_acks"], hosp


def test_rejection_reasons_carried_through(flow):
    conn, _registry, manifest, _acks = flow
    now = datetime.fromisoformat(manifest.report_now_utc)
    report = track_acks(conn, _registry, now)
    rejected = [o for o in report.outcomes if o.state == "REJECTED"]
    assert rejected and all(o.reason == "Patient not on file" for o in rejected)


def test_corrected_report_updated_in_place(flow):
    conn, _registry, manifest, _acks = flow
    for defect in manifest.by_category("corrected_report"):
        row = conn.execute(
            "SELECT status, report_text FROM reports WHERE hospital_id=? AND accession=?",
            (defect.hospital_id, defect.accession),
        ).fetchone()
        assert row is not None and row["status"] == "C"
        assert "ADDENDUM" in row["report_text"]
    n = conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
    assert n == manifest.expected["fhir"]["diagnostic_reports"]


def test_merges_applied(flow):
    conn, _registry, _manifest, _acks = flow
    merged = conn.execute(
        "SELECT hospital_id, mrn, merged_into_mrn FROM patients"
        " WHERE merged_into_mrn IS NOT NULL ORDER BY hospital_id"
    ).fetchall()
    assert [(r["hospital_id"]) for r in merged] == ["HOSPA", "HOSPC"]


def test_tat_matches_manifest(flow):
    from hl7bridge.synth.generator import REPORT_MONTH
    from hl7bridge.tat import tat_report

    conn, registry, manifest, _acks = flow
    reports = tat_report(conn, registry, REPORT_MONTH)
    for hosp, expected in manifest.expected["tat"].items():
        r = reports[hosp]
        got = {
            "finals": r.finals,
            "measured": len(r.measured),
            "missing_exam_time": r.missing_exam_time,
            "missing_final_time": r.missing_final_time,
            "missing_order": r.missing_order,
            "prelims": r.prelims,
            "corrected": r.corrected,
            "breaches": r.breaches,
        }
        assert got == expected, hosp
        # Stat exams measure against the stat target, not the default.
        stat_exams = [m for m in r.measured if m.priority == "S"]
        assert stat_exams, hosp
        assert all(
            m.target_hours == registry.by_id[hosp].tat.target_hours("S") for m in stat_exams
        ), hosp
