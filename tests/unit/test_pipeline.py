"""Pipeline: every row of the ACK decision table, tested with a real store."""

from datetime import UTC, datetime

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.ingest.parser import get_field, parse_hl7
from hl7bridge.ingest.pipeline import IngestPipeline
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.store import connect, quarantine_summary

CONFIG_DIR = BUNDLED_PROFILES
FIXED_NOW = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)


@pytest.fixture
def pipeline(tmp_path):
    conn = connect(tmp_path / "p.db")
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    yield IngestPipeline(registry, conn, clock=lambda: FIXED_NOW)
    conn.close()


def ack_fields(ack_text: str) -> tuple[str, str]:
    """(MSA-1 ack code, MSA-2 original control id) from the returned ACK."""
    m = parse_hl7(ack_text)
    return get_field(m, "MSA-1"), get_field(m, "MSA-2")


def msg(
    *segments: str,
    msg_type: str = "ORM^O01",
    control_id: str = "C1",
    facility: str = "HOSPA",
    ts: str = "20261007100000",
) -> bytes:
    msh = f"MSH|^~\\&|RIS|{facility}|BRIDGE|HUB|{ts}||{msg_type}|{control_id}|P|2.3"
    return ("\r".join([msh, *segments]) + "\r").encode()


PID = "PID|1||M1001^^^HOSPA^MR||ZZZTEST^ALPHA||19700101|F"
ORC_NW = "ORC|NW|PL1|FIL1||||||20261007093000"


def obr(
    accession: str = "ACC123",
    observed: str = "20261007100000",
    result_ts: str = "",
    modality: str = "CT",
    status: str = "",
) -> str:
    """Build an OBR segment with exact spec positions (OBR-25 = status)."""
    fields = [""] * 26
    fields[0] = "OBR"
    fields[1] = "1"
    fields[2] = "PL1"
    fields[3] = accession
    fields[4] = "71250^CT CHEST^local"
    fields[7] = observed
    fields[22] = result_ts
    fields[24] = modality
    fields[25] = status
    return "|".join(fields)


OBR = obr()


def test_valid_orm_is_aa_and_stored(pipeline):
    ack = pipeline.handle_bytes(msg(PID, ORC_NW, OBR))
    assert ack_fields(ack) == ("AA", "C1")
    row = pipeline.conn.execute("SELECT * FROM orders").fetchone()
    assert row["accession"] == "ACC123"
    assert row["modality"] == "CT"
    assert row["status"] == "new"
    # ORC-9 09:30 America/Los_Angeles == 16:30 UTC
    assert row["ordered_at_utc"] == "2026-10-07T16:30:00+00:00"
    assert quarantine_summary(pipeline.conn) == {}


def test_accession_field_respects_hospital_dialect(pipeline):
    # HOSPB expects the accession in ORC-3; the same OBR-3 placement that
    # works for HOSPA must NOT satisfy HOSPB.
    orc_no_filler = "ORC|NW|PL1|||||||20261007093000"
    obr_with_acc = OBR
    ack = pipeline.handle_bytes(
        msg(PID, orc_no_filler, obr_with_acc, facility="HOSPB", control_id="CB1")
    )
    assert ack_fields(ack)[0] == "AE"
    assert quarantine_summary(pipeline.conn) == {"missing_accession": 1}
    # With ORC-3 filled, HOSPB accepts and the accession comes from ORC-3.
    ack = pipeline.handle_bytes(msg(PID, ORC_NW, OBR, facility="HOSPB", control_id="CB2"))
    assert ack_fields(ack)[0] == "AA"
    row = pipeline.conn.execute("SELECT accession FROM orders WHERE hospital_id='HOSPB'").fetchone()
    assert row["accession"] == "FIL1"


def test_not_utf8_is_ar_parse_error(pipeline):
    ack = pipeline.handle_bytes(b"\xff\xfe\x00 garbage")
    code, orig = ack_fields(ack)
    assert (code, orig) == ("AR", "UNKNOWN")
    assert quarantine_summary(pipeline.conn) == {"parse_error": 1}


def test_unparseable_is_ar_parse_error(pipeline):
    ack = pipeline.handle_bytes(b"this is not hl7")
    assert ack_fields(ack) == ("AR", "UNKNOWN")
    assert quarantine_summary(pipeline.conn) == {"parse_error": 1}


def test_missing_control_id_is_ar(pipeline):
    raw = msg(PID, ORC_NW, OBR).decode().replace("|C1|", "||").encode()
    ack = pipeline.handle_bytes(raw)
    assert ack_fields(ack) == ("AR", "UNKNOWN")
    assert quarantine_summary(pipeline.conn) == {"missing_control_id": 1}


def test_unknown_hospital_is_ar(pipeline):
    ack = pipeline.handle_bytes(msg(PID, ORC_NW, OBR, facility="NOWHERE"))
    assert ack_fields(ack) == ("AR", "C1")
    assert quarantine_summary(pipeline.conn) == {"unknown_hospital": 1}


def test_unexpected_type_is_ar(pipeline):
    ack = pipeline.handle_bytes(msg(PID, msg_type="SIU^S12"))
    assert ack_fields(ack) == ("AR", "C1")
    assert quarantine_summary(pipeline.conn) == {"unexpected_message_type": 1}


def test_missing_accession_is_ae(pipeline):
    obr_no_acc = "OBR|1|PL1||71250^CT CHEST^local|||20261007100000|||||||||||||||||CT"
    ack = pipeline.handle_bytes(msg(PID, ORC_NW, obr_no_acc))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"missing_accession": 1}
    assert pipeline.conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0
    # Validation failure must also roll back the messages row (no AA record).
    assert pipeline.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


def test_bad_timestamp_is_ae(pipeline):
    bad_orc = "ORC|NW|PL1|FIL1||||||2026131599"
    ack = pipeline.handle_bytes(msg(PID, bad_orc, OBR))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"bad_timestamp": 1}


def test_unknown_report_status_is_ae_not_guessed(pipeline):
    obr_bad_status = obr(result_ts="20261007103000", status="X")
    ack = pipeline.handle_bytes(msg(PID, "ORC|RE|PL1|FIL1", obr_bad_status, msg_type="ORU^R01"))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"unknown_report_status": 1}
    assert pipeline.conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"] == 0


def test_valid_oru_stores_report_text(pipeline):
    obr_final = obr(result_ts="20261007103000", status="F")
    obx = "OBX|1|TX|IMPRESSION||No acute findings.||||||F"
    ack = pipeline.handle_bytes(msg(PID, "ORC|RE|PL1|FIL1", obr_final, obx, msg_type="ORU^R01"))
    assert ack_fields(ack) == ("AA", "C1")
    row = pipeline.conn.execute("SELECT * FROM reports").fetchone()
    assert row["status"] == "F"
    assert row["report_text"] == "No acute findings."
    assert row["result_at_utc"] == "2026-10-07T17:30:00+00:00"  # 10:30 PDT


def test_duplicate_control_id_reacked_once_stored(pipeline):
    payload = msg(PID, ORC_NW, OBR)
    assert ack_fields(pipeline.handle_bytes(payload))[0] == "AA"
    assert ack_fields(pipeline.handle_bytes(payload))[0] == "AA"
    counts = pipeline.conn.execute(
        "SELECT COUNT(*) AS n, SUM(duplicate_count) AS d FROM messages"
    ).fetchone()
    assert (counts["n"], counts["d"]) == (1, 1)
    assert pipeline.conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 1


def test_adt_a04_upserts_patient(pipeline):
    ack = pipeline.handle_bytes(msg(PID, msg_type="ADT^A04"))
    assert ack_fields(ack)[0] == "AA"
    row = pipeline.conn.execute("SELECT * FROM patients").fetchone()
    assert (row["mrn"], row["family_name"], row["birth_date"]) == ("M1001", "ZZZTEST", "1970-01-01")


def test_adt_a40_records_merge(pipeline):
    pipeline.handle_bytes(msg(PID, msg_type="ADT^A04", control_id="C1"))
    mrg = "MRG|M2002^^^HOSPA^MR"
    ack = pipeline.handle_bytes(msg(PID, mrg, msg_type="ADT^A40", control_id="C2"))
    assert ack_fields(ack)[0] == "AA"
    row = pipeline.conn.execute("SELECT * FROM merges").fetchone()
    assert (row["surviving_mrn"], row["merged_mrn"]) == ("M1001", "M2002")


def test_adt_a40_without_mrg_is_ae(pipeline):
    ack = pipeline.handle_bytes(msg(PID, msg_type="ADT^A40"))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"missing_merge_mrn": 1}


def test_order_cancel_flow(pipeline):
    pipeline.handle_bytes(msg(PID, ORC_NW, OBR, control_id="C1"))
    orc_ca = "ORC|CA|PL1|FIL1||||||20261007110000"
    ack = pipeline.handle_bytes(msg(PID, orc_ca, OBR, control_id="C2"))
    assert ack_fields(ack)[0] == "AA"
    assert pipeline.conn.execute("SELECT status FROM orders").fetchone()["status"] == "cancelled"


def test_unsupported_order_control_is_ae(pipeline):
    orc_xx = "ORC|XX|PL1|FIL1||||||20261007093000"
    ack = pipeline.handle_bytes(msg(PID, orc_xx, OBR))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"unsupported_order_control": 1}


def test_inbound_ack_stored(pipeline):
    ack_msg = msg("MSA|AR|ORU0001|Patient not on file", msg_type="ACK^R01", control_id="CA1")
    ack = pipeline.handle_bytes(ack_msg)
    assert ack_fields(ack)[0] == "AA"
    row = pipeline.conn.execute("SELECT * FROM acks_in").fetchone()
    assert (row["msa_code"], row["msa_control_id"], row["reason"]) == (
        "AR",
        "ORU0001",
        "Patient not on file",
    )


def test_ack_without_msa_is_ae(pipeline):
    ack = pipeline.handle_bytes(msg(msg_type="ACK^R01", control_id="CA2"))
    assert ack_fields(ack) == ("AE", "CA2")
    assert quarantine_summary(pipeline.conn) == {"missing_msa": 1}


def test_timezone_dialect_offset_wins(pipeline):
    # HOSPC sends explicit offsets; the offset must win over hospital tz.
    orc = "ORC|NW|PL1|FIL1||||||20261007093000+0000"
    ack = pipeline.handle_bytes(msg(PID, orc, OBR, facility="HOSPC"))
    assert ack_fields(ack)[0] == "AA"
    row = pipeline.conn.execute(
        "SELECT ordered_at_utc FROM orders WHERE hospital_id='HOSPC'"
    ).fetchone()
    assert row["ordered_at_utc"] == "2026-10-07T09:30:00+00:00"


def test_mllp_junk_is_quarantined(pipeline):
    import asyncio

    asyncio.run(pipeline.handle_junk(b"stray bytes outside framing"))
    assert quarantine_summary(pipeline.conn) == {"mllp_junk": 1}


def test_internal_error_boundary_is_loud_ar(pipeline, caplog):
    import asyncio

    def boom():
        raise RuntimeError("injected clock failure")

    broken = IngestPipeline(pipeline.registry, pipeline.conn, clock=boom)
    # First call of handle_bytes raises inside; handle_frame must still ACK.
    # Give the boundary a working clock for its own ACK by failing only once.
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected failure")
        return FIXED_NOW

    broken.clock = flaky
    with caplog.at_level("ERROR"):
        ack_bytes = asyncio.run(broken.handle_frame(msg(PID, ORC_NW, OBR)))
    assert ack_fields(ack_bytes.decode())[0] == "AR"
    assert "internal error" in caplog.text
    assert quarantine_summary(pipeline.conn) == {"internal_error": 1}


# -- remaining branches of the decision table ---------------------------------


def test_missing_mrn_is_ae(pipeline):
    pid_no_mrn = "PID|1||||ZZZTEST^ALPHA||19700101|F"
    ack = pipeline.handle_bytes(msg(pid_no_mrn, ORC_NW, OBR))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"missing_mrn": 1}


def test_invalid_msa_code_is_ae(pipeline):
    ack = pipeline.handle_bytes(msg("MSA|XX|ORU0001", msg_type="ACK^R01", control_id="CA3"))
    assert ack_fields(ack) == ("AE", "CA3")
    assert quarantine_summary(pipeline.conn) == {"missing_msa": 1}
    assert pipeline.conn.execute("SELECT COUNT(*) AS n FROM acks_in").fetchone()["n"] == 0


def test_malformed_birthdate_is_ae(pipeline):
    pid_bad_dob = "PID|1||M1001^^^HOSPA^MR||ZZZTEST^ALPHA||banana|F"
    ack = pipeline.handle_bytes(msg(pid_bad_dob, msg_type="ADT^A04"))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"bad_timestamp": 1}


def test_orm_with_no_timestamp_anywhere_is_ae(pipeline):
    orc_no_ts = "ORC|NW|PL1|FIL1"
    ack = pipeline.handle_bytes(msg(PID, orc_no_ts, OBR, ts=""))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"bad_timestamp": 1}


def test_cancel_for_unknown_order_creates_tombstone(pipeline):
    orc_ca = "ORC|CA|PL1|FIL1||||||20261007110000"
    ack = pipeline.handle_bytes(msg(PID, orc_ca, OBR))
    assert ack_fields(ack) == ("AA", "C1")
    row = pipeline.conn.execute("SELECT status, accession FROM orders").fetchone()
    assert (row["status"], row["accession"]) == ("cancelled", "ACC123")


def test_expected_type_without_handler_is_ae(pipeline, tmp_path):
    """Config lists a type the code has no handler for: loud config/code
    mismatch, not a silent accept."""
    cfg_text = (CONFIG_DIR / "hospa.yaml").read_text(encoding="utf-8")
    needle = "expected_message_types: [ADT^A04, ADT^A08, ADT^A40, ORM^O01, ORU^R01]"
    assert needle in cfg_text
    (tmp_path / "h.yaml").write_text(
        cfg_text.replace(needle, "expected_message_types: [SIU^S12]"), encoding="utf-8"
    )
    registry = HospitalRegistry.load_dir(tmp_path)
    p2 = IngestPipeline(registry, pipeline.conn, clock=lambda: FIXED_NOW)
    ack = p2.handle_bytes(msg(PID, msg_type="SIU^S12"))
    assert ack_fields(ack) == ("AE", "C1")
    assert quarantine_summary(pipeline.conn) == {"no_handler_for_type": 1}


# -- visits (PV1) and message references --------------------------------------------


def _pv1(**fields: str) -> str:
    values = [""] * 46
    values[0] = "PV1"
    values[1] = "1"
    for key, value in fields.items():
        values[int(key[1:])] = value
    return "|".join(values).rstrip("|")


def _adt(event: str, pv1: str, control_id: str) -> bytes:
    return msg(PID, pv1, msg_type=f"ADT^{event}", control_id=control_id, facility="HOSPC")


def test_a01_stores_the_visit_and_a08_updates_it(pipeline):
    a01 = _adt("A01", _pv1(p2="I", p19="V1", p44="20261007080000-0400"), "C1")
    a08 = _adt("A08", _pv1(p2="I", p19="V1", p44="20261007080000-0400", p45="20261008090000"), "C2")
    assert ack_fields(pipeline.handle_bytes(a01))[0] == "AA"
    assert ack_fields(pipeline.handle_bytes(a08))[0] == "AA"
    rows = pipeline.conn.execute("SELECT * FROM encounters").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["visit_number"], row["patient_class"], row["patient_mrn"]) == ("V1", "I", "M1001")
    assert row["admitted_at_utc"] == "2026-10-07T12:00:00+00:00"
    # No offset: read in the hospital's zone (America/New_York, EDT).
    assert row["discharged_at_utc"] == "2026-10-08T13:00:00+00:00"
    last = pipeline.conn.execute("SELECT id FROM messages WHERE control_id = 'C2'").fetchone()
    assert row["last_message_id"] == last["id"]


def test_adt_without_a_visit_number_stores_no_visit(pipeline):
    assert ack_fields(pipeline.handle_bytes(_adt("A04", _pv1(p2="O"), "C1")))[0] == "AA"
    assert pipeline.conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] == 0


def test_a40_never_writes_a_visit(pipeline):
    a40 = msg(
        PID, "MRG|M2^^^HOSPC^MR", _pv1(p2="I", p19="V9"), msg_type="ADT^A40", facility="HOSPC"
    )
    assert ack_fields(pipeline.handle_bytes(a40))[0] == "AA"
    assert pipeline.conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["p44", "p45"])
def test_bad_visit_time_is_ae_and_writes_nothing(pipeline, field):
    raw = _adt("A01", _pv1(p2="I", p19="V1", **{field: "2026"}), "C1")
    ack = parse_hl7(pipeline.handle_bytes(raw))
    assert get_field(ack, "MSA-1") == "AE"
    assert get_field(ack, "MSA-3") == f"bad_timestamp: PV1-{field[1:]}"
    assert pipeline.conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] == 0
    assert pipeline.conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0


def test_unknown_patient_class_is_stored_as_sent(pipeline):
    """The store keeps what was sent; fhir sync refuses a class it cannot map."""
    assert ack_fields(pipeline.handle_bytes(_adt("A01", _pv1(p2="Z", p19="V1"), "C1")))[0] == "AA"
    assert pipeline.conn.execute("SELECT patient_class FROM encounters").fetchone()[0] == "Z"


def test_reports_carry_the_message_row_id(pipeline):
    raw = msg(
        PID,
        "ORC|RE|PL1",
        obr(status="F", result_ts="20261007110000"),
        "OBX|1|TX|RPT^T||text||||||F",
        msg_type="ORU^R01",
        control_id="R1",
    )
    assert ack_fields(pipeline.handle_bytes(raw))[0] == "AA"
    message_id = pipeline.conn.execute("SELECT id FROM messages").fetchone()["id"]
    report = pipeline.conn.execute("SELECT * FROM reports").fetchone()
    event = pipeline.conn.execute("SELECT * FROM report_events").fetchone()
    assert report["message_id"] == event["message_id"] == message_id
    assert report["observed_at_utc"] == "2026-10-07T17:00:00+00:00"


def test_placer_falls_back_to_obr2(pipeline):
    raw = msg(PID, "ORC|NW||FIL1||||||20261007093000", OBR, control_id="C9")
    assert ack_fields(pipeline.handle_bytes(raw))[0] == "AA"
    assert pipeline.conn.execute("SELECT placer_order_id FROM orders").fetchone()[0] == "PL1"
