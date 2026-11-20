"""ACK tracker: one terminal state per outbound report, loud edge handling."""

from datetime import UTC, datetime, timedelta

import pytest

from hl7bridge.acks import track_acks
from hl7bridge.config import HospitalRegistry
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.store import connect, utc_iso
from hl7bridge.store.repo import AckInRow, insert_ack_in, record_outbound_report

CONFIG_DIR = BUNDLED_PROFILES
SENT = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)


@pytest.fixture
def registry():
    return HospitalRegistry.load_dir(CONFIG_DIR)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "a.db")
    yield c
    c.close()


def outbound(conn, control_id, *, hosp="HOSPA", sent=SENT):
    record_outbound_report(
        conn,
        hospital_id=hosp,
        control_id=control_id,
        accession=f"ACC-{control_id}",
        sent_at_utc=utc_iso(sent),
    )


def ack(conn, msa_control, code, *, hosp="HOSPA", at=None, reason=None, ack_ctl="A1"):
    insert_ack_in(
        conn,
        AckInRow(
            hospital_id=hosp,
            ack_control_id=ack_ctl,
            msa_code=code,
            msa_control_id=msa_control,
            reason=reason,
            received_at_utc=utc_iso(at or (SENT + timedelta(minutes=10))),
        ),
    )


def states_of(report):
    return {o.control_id: o.state for o in report.outcomes}


def test_terminal_states(conn, registry):
    with conn:
        outbound(conn, "R1")
        ack(conn, "R1", "AA")
        outbound(conn, "R2")
        ack(conn, "R2", "AE", reason="Application error")
        outbound(conn, "R3")
        ack(conn, "R3", "AR", reason="Patient not on file")
    report = track_acks(conn, registry, SENT + timedelta(hours=2))
    assert states_of(report) == {"R1": "DELIVERED", "R2": "ERRORED", "R3": "REJECTED"}
    assert report.outcomes[1].reason == "Application error"


def test_no_ack_uses_injected_clock_and_hospital_timeout(conn, registry):
    # HOSPA timeout 30 min, HOSPB timeout 45 min.
    with conn:
        outbound(conn, "RA", hosp="HOSPA")
        outbound(conn, "RB", hosp="HOSPB")
    at_35 = SENT + timedelta(minutes=35)
    report = track_acks(conn, registry, at_35)
    assert states_of(report) == {"RA": "NO_ACK", "RB": "PENDING"}
    at_50 = SENT + timedelta(minutes=50)
    report = track_acks(conn, registry, at_50)
    assert states_of(report) == {"RA": "NO_ACK", "RB": "NO_ACK"}


def test_duplicate_acks_counted_first_wins(conn, registry):
    with conn:
        outbound(conn, "R1")
        ack(conn, "R1", "AA", at=SENT + timedelta(minutes=5), ack_ctl="A1")
        ack(conn, "R1", "AA", at=SENT + timedelta(minutes=6), ack_ctl="A2")
    report = track_acks(conn, registry, SENT + timedelta(hours=1))
    o = report.outcomes[0]
    assert (o.state, o.duplicate_acks, o.conflicting_codes) == ("DELIVERED", 1, ())


def test_conflicting_later_ack_is_reported_not_reconciled(conn, registry):
    with conn:
        outbound(conn, "R1")
        ack(conn, "R1", "AA", at=SENT + timedelta(minutes=5))
        ack(conn, "R1", "AR", at=SENT + timedelta(minutes=9), ack_ctl="A2")
    report = track_acks(conn, registry, SENT + timedelta(hours=1))
    o = report.outcomes[0]
    assert o.state == "DELIVERED"  # first ACK decides
    assert o.conflicting_codes == ("AR",)


def test_out_of_order_arrival_first_by_received_time_wins(conn, registry):
    with conn:
        outbound(conn, "R1")
        # Inserted later but received earlier: must win.
        ack(conn, "R1", "AE", at=SENT + timedelta(minutes=9), ack_ctl="A2")
        ack(conn, "R1", "AA", at=SENT + timedelta(minutes=3), ack_ctl="A1")
    report = track_acks(conn, registry, SENT + timedelta(hours=1))
    assert report.outcomes[0].state == "DELIVERED"


def test_unknown_control_id_listed_not_dropped(conn, registry):
    with conn:
        outbound(conn, "R1")
        ack(conn, "R1", "AA")
        ack(conn, "GHOST", "AA", ack_ctl="A9")
    report = track_acks(conn, registry, SENT + timedelta(hours=1))
    assert len(report.unknown_acks) == 1
    assert report.unknown_acks[0]["msa_control_id"] == "GHOST"


def test_every_outbound_gets_exactly_one_state(conn, registry):
    with conn:
        for i in range(6):
            outbound(conn, f"R{i}")
        ack(conn, "R0", "AA")
        ack(conn, "R1", "AE")
        ack(conn, "R2", "AR")
        ack(conn, "R3", "AA")
        ack(conn, "R3", "AA", ack_ctl="A2")
    report = track_acks(conn, registry, SENT + timedelta(hours=1))
    assert len(report.outcomes) == 6
    assert len({o.control_id for o in report.outcomes}) == 6
    by_state = report.by_state()
    assert sum(len(v) for v in by_state.values()) == 6


def test_naive_now_rejected(conn, registry):
    with pytest.raises(ValueError, match="aware"):
        track_acks(conn, registry, datetime(2026, 10, 8, 12, 0))
