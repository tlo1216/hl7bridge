"""TAT engine: hand-computed statistics, DST arithmetic, missing-data accounting.

Every expected number in this file was computed by hand from the fixture
timestamps, so the engine is checked against numbers it did not produce.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.store import connect, utc_iso
from hl7bridge.store.repo import (
    OrderRow,
    ReportEventRow,
    insert_report_event,
    message_row_id,
    record_message,
    upsert_order,
)
from hl7bridge.tat import render_tat_text, tat_report

CONFIG_DIR = BUNDLED_PROFILES
CHICAGO = ZoneInfo("America/Chicago")
BASE = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)


@pytest.fixture
def registry():
    return HospitalRegistry.load_dir(CONFIG_DIR)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def add_order(conn, accession, *, modality="CT", priority="R", ordered=BASE, hosp="HOSPA"):
    upsert_order(
        conn,
        OrderRow(
            hospital_id=hosp,
            accession=accession,
            placer_order_id=None,
            procedure_code="71250",
            procedure_description="CT CHEST",
            code_system="local",
            modality=modality,
            priority=priority,
            patient_mrn="M1",
            ordered_at_utc=utc_iso(ordered),
            status="new",
        ),
    )


def add_event(conn, accession, status, *, observed, result, hosp="HOSPA", control="C1"):
    record_message(
        conn,
        hospital_id=hosp,
        control_id=control,
        message_type="ORU^R01",
        received_at_utc=utc_iso(BASE),
        raw="MSH|^~\\&|\r",
    )
    insert_report_event(
        conn,
        ReportEventRow(
            hospital_id=hosp,
            accession=accession,
            control_id=control,
            message_id=message_row_id(conn, hospital_id=hosp, control_id=control),
            status=status,
            observed_at_utc=utc_iso(observed) if observed else None,
            result_at_utc=utc_iso(result) if result else None,
            received_at_utc=utc_iso(result or BASE),
        ),
    )


def run(conn, registry, month="2026-10"):
    return tat_report(conn, registry, month)


def test_exact_median_and_p90_hand_computed(conn, registry):
    # Five exams, exam_to_final of exactly 1.0h, 2.0h, 3.0h, 4.0h, 5.0h.
    # Hand computation: median = 3.0. P90 with inclusive quantiles on
    # n=5: position 0.9*(5-1) = 3.6 -> 4.0 + 0.6*(5.0-4.0) = 4.6.
    for k in range(1, 6):
        acc = f"A{k}"
        add_order(conn, acc, ordered=BASE - timedelta(hours=1))
        exam = BASE + timedelta(minutes=10 * k)
        add_event(conn, acc, "F", observed=exam, result=exam + timedelta(hours=k))
    r = run(conn, registry)["HOSPA"]
    assert len(r.measured) == 5
    g = r.groups[("CT", "R")]
    assert g.n == 5
    assert g.median_hours == pytest.approx(3.0, abs=1e-12)
    assert g.p90_hours == pytest.approx(4.6, abs=1e-12)
    # Target for routine CT at HOSPA is 2.0h; exams at 3h, 4h, 5h breach.
    assert g.breaches == 3
    assert r.breaches == 3


def test_full_precision_no_premature_rounding(conn, registry):
    # 1 minute = 0.01666...h; two exams at 101 and 103 minutes. The median
    # is 102 minutes = 1.7h exactly; any rounding at input would corrupt it.
    for k, minutes in enumerate((101, 103), start=1):
        acc = f"P{k}"
        add_order(conn, acc)
        exam = BASE
        add_event(conn, acc, "F", observed=exam, result=exam + timedelta(minutes=minutes))
    g = run(conn, registry)["HOSPA"].groups[("CT", "R")]
    assert g.median_hours == pytest.approx(1.7, abs=1e-12)
    assert g.median_hours != 1.68 and g.median_hours != 1.72


def test_single_exam_p90_equals_value(conn, registry):
    add_order(conn, "S1")
    add_event(conn, "S1", "F", observed=BASE, result=BASE + timedelta(hours=1.5))
    g = run(conn, registry)["HOSPA"].groups[("CT", "R")]
    assert (g.n, g.median_hours, g.p90_hours) == (1, 1.5, 1.5)


def test_stat_priority_uses_its_own_target(conn, registry):
    # HOSPA: routine target 2h, stat target 1h. A 1.5h exam breaches only stat.
    add_order(conn, "R1", priority="R")
    add_event(conn, "R1", "F", observed=BASE, result=BASE + timedelta(hours=1.5))
    add_order(conn, "S1", priority="S")
    add_event(conn, "S1", "F", observed=BASE, result=BASE + timedelta(hours=1.5), control="C2")
    r = run(conn, registry)["HOSPA"]
    assert r.groups[("CT", "R")].breaches == 0
    assert r.groups[("CT", "S")].breaches == 1


def test_on_target_is_not_a_breach(conn, registry):
    add_order(conn, "E1")
    add_event(conn, "E1", "F", observed=BASE, result=BASE + timedelta(hours=2))
    assert run(conn, registry)["HOSPA"].breaches == 0


def test_dst_spring_forward_elapsed_time_is_real_not_wall_clock(conn, registry):
    # HOSPB is America/Chicago. Exam 2026-03-08 01:30 CST, final 03:30 CDT.
    # Wall clock difference is 2h; real elapsed time is 1h.
    exam = datetime(2026, 3, 8, 1, 30, tzinfo=CHICAGO)
    final = datetime(2026, 3, 8, 3, 30, tzinfo=CHICAGO)
    # NOTE: subtracting two datetimes that SHARE a ZoneInfo is wall-clock
    # arithmetic in Python (2h here); only after conversion to UTC is the
    # real elapsed time visible. The store's UTC-at-the-door invariant is
    # what protects the engine from this trap.
    assert final - exam == timedelta(hours=2)
    assert final.astimezone(UTC) - exam.astimezone(UTC) == timedelta(hours=1)
    add_order(conn, "D1", hosp="HOSPB", ordered=exam - timedelta(hours=1))
    add_event(conn, "D1", "F", observed=exam, result=final, hosp="HOSPB")
    g = run(conn, registry, month="2026-03")["HOSPB"].groups[("CT", "R")]
    assert g.median_hours == pytest.approx(1.0, abs=1e-12)


def test_corrected_event_does_not_move_final_time(conn, registry):
    add_order(conn, "C1")
    add_event(conn, "C1", "F", observed=BASE, result=BASE + timedelta(hours=1))
    add_event(conn, "C1", "C", observed=BASE, result=BASE + timedelta(hours=5), control="C2")
    r = run(conn, registry)["HOSPA"]
    assert r.corrected == 1
    assert r.measured[0].exam_to_final_hours == pytest.approx(1.0)


def test_prelim_interval_and_counts(conn, registry):
    add_order(conn, "PR1")
    add_event(conn, "PR1", "P", observed=BASE, result=BASE + timedelta(minutes=30))
    add_event(conn, "PR1", "F", observed=BASE, result=BASE + timedelta(hours=2), control="C2")
    r = run(conn, registry)["HOSPA"]
    assert r.prelims == 1
    assert r.measured[0].exam_to_prelim_hours == pytest.approx(0.5)


def test_missing_exam_time_counted_not_dropped(conn, registry):
    add_order(conn, "M1")
    add_event(conn, "M1", "F", observed=None, result=BASE + timedelta(hours=9))
    r = run(conn, registry)["HOSPA"]
    assert (r.finals, len(r.measured), r.missing_exam_time) == (1, 0, 1)
    # A 9h unmeasurable report must NOT appear as a breach.
    assert r.breaches == 0


def test_missing_final_time_counted(conn, registry):
    add_order(conn, "M2")
    add_event(conn, "M2", "F", observed=BASE, result=None)
    r = run(conn, registry)["HOSPA"]
    assert (r.finals, r.missing_final_time, len(r.measured)) == (1, 1, 0)


def test_report_without_order_counted_and_grouped_unspecified(conn, registry):
    add_event(conn, "NOORD", "F", observed=BASE, result=BASE + timedelta(hours=1))
    r = run(conn, registry)["HOSPA"]
    assert r.missing_order == 1
    assert ("unknown", "unspecified") in r.groups
    assert r.measured[0].order_to_exam_hours is None


def test_month_filter_uses_hospital_local_time(conn, registry):
    # 2026-11-01 04:30 UTC is 2026-10-31 21:30 in Los Angeles (HOSPA):
    # this final belongs to October locally despite a November UTC stamp.
    final = datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
    add_order(conn, "MO1")
    add_event(conn, "MO1", "F", observed=final - timedelta(hours=1), result=final)
    assert run(conn, registry, month="2026-10")["HOSPA"].finals == 1
    assert run(conn, registry, month="2026-11")["HOSPA"].finals == 0


def test_bad_month_argument_rejected(conn, registry):
    with pytest.raises(ValueError, match="YYYY-MM"):
        tat_report(conn, registry, "October 2026")


def test_render_contains_summary_sentence(conn, registry):
    add_order(conn, "R1")
    add_event(conn, "R1", "F", observed=BASE, result=BASE + timedelta(hours=3))
    text = render_tat_text(run(conn, registry))
    assert "HOSPA, 2026-10: 1 finalized exams measured" in text
    assert "median exam-to-final 3.00h" in text
    assert "1 target breach(es)" in text


# -- regressions: NULL-final ordering, month attribution, target parity -------


def test_null_result_final_does_not_displace_dated_final(conn, registry):
    """T2: a timestamp-less F retransmission must not hide the dated final."""
    add_order(conn, "T2")
    add_event(conn, "T2", "F", observed=BASE, result=BASE + timedelta(hours=1))
    # Later event, no OBR-22; previously SQLite's NULLs-first ordering made
    # this the 'first' final and the accession read as missing_final_time.
    add_event(conn, "T2", "F", observed=BASE, result=None, control="C2")
    r = run(conn, registry)["HOSPA"]
    assert (r.finals, len(r.measured), r.missing_final_time) == (1, 1, 0)
    assert r.measured[0].exam_to_final_hours == pytest.approx(1.0)


def test_unplaceable_final_lands_in_exactly_one_month(conn, registry):
    """T4: a final with no result time is attributed by received time, once."""
    add_order(conn, "T4")
    add_event(conn, "T4", "F", observed=BASE, result=None)  # received at BASE (Oct)
    for month, finals, missing in (("2026-10", 1, 1), ("2026-09", 0, 0), ("2026-11", 0, 0)):
        r = run(conn, registry, month=month)["HOSPA"]
        assert (r.finals, r.missing_final_time) == (finals, missing), month


def test_routine_priority_honors_by_priority_override(conn, registry, tmp_path):
    """T3: an 'R' entry in the by-priority map must apply to routine exams."""
    cfg_text = (CONFIG_DIR / "hospa.yaml").read_text(encoding="utf-8")
    needle = "  exam_to_final_target_hours_by_priority:\n    S: 1"
    assert needle in cfg_text
    (tmp_path / "h.yaml").write_text(
        cfg_text.replace(needle, needle + "\n    R: 4"), encoding="utf-8"
    )
    custom = HospitalRegistry.load_dir(tmp_path)
    add_order(conn, "T3", priority="R")
    add_event(conn, "T3", "F", observed=BASE, result=BASE + timedelta(hours=3))
    r = tat_report(conn, custom, "2026-10")["HOSPA"]
    assert r.measured[0].target_hours == 4.0
    assert r.breaches == 0  # 3h < 4h override; default 2h would have breached
