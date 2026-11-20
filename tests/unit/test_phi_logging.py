"""No PHI in logs or error text.

Every test here captures the ROOT logger at DEBUG, so third-party loggers
(httpx, httpcore) are included, then scans the captured text for canaries:

- every patient name, MRN, birth date, accession, study UID, control id and
  report phrase the synthetic corpus holds, read back from the store, plus
- hand-made canaries planted in hostile places (MSH-10, MSH-9, a broken
  segment, an exception message, a FHIR server body, a transport error).

The scan runs on the raw text and on its URL-decoded form, because httpx
logs URLs percent-encoded (``%7CMA1001``), where a word-boundary regex
would miss the MRN.

Each test also asserts that logging DID happen, so none can pass by being
silent. Reports printed to stdout are product output and may hold
identifiers; they are not checked here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from hl7bridge import cli
from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import BridgeError, FhirPostError, HL7ParseError
from hl7bridge.fhir import sync_to_fhir
from hl7bridge.fhir.client import HapiClient
from hl7bridge.ingest import IngestPipeline
from hl7bridge.ingest import pipeline as pipeline_module
from hl7bridge.ingest.loaders import load_outbound, load_studies
from hl7bridge.ingest.parser import get_field, parse_hl7
from hl7bridge.logsafety import (
    THIRD_PARTY_HTTP_LOGGERS,
    UrlQueryRedactor,
    configure_logging,
    redact_url_queries,
    short_hash,
)
from hl7bridge.store import connect
from hl7bridge.store.repo import OrderRow, StudyRow, insert_study, upsert_order
from hl7bridge.synth import generate
from hl7bridge.synth.generator import (
    BAD_CLOCK_LINE,
    CORRECTED_LINE,
    IMPRESSION,
    N_PATIENTS,
    REPORT_LINES,
)
from hl7bridge.synth.patients import FAMILY_NAMES, GIVEN_NAMES, make_pool

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "hospitals"
# No leading word boundary: percent-encoded URLs put a hex digit right
# before the value (%7CMA1001).
MRN_PATTERN = re.compile(r"M[ABC]1\d{3}(?!\d)")
ACCESSION_PATTERN = re.compile(r"ACCX?[ABC]\d{4}(?!\d)")

# Hand-made canaries, shaped like the synthetic pools so the patterns catch
# them as well as the exact-value scan.
CANARY_MRN = "MA1987"
CANARY_MRN_ORPHAN = "MA1986"  # an order points at it, no patient row exists
CANARY_ACC = "ACCA9876"
CANARY_STUDY_UID = "2.25.314159265358979323846264338327950288"
CANARY_NAME = "ZZZCANARY"
CANARIES = (CANARY_MRN, CANARY_MRN_ORPHAN, CANARY_ACC, CANARY_STUDY_UID, CANARY_NAME)

FHIR_BASE = "http://fhir.test/fhir"

Handler = Callable[[httpx.Request], httpx.Response]


# -- helpers -----------------------------------------------------------------


def _birth_dates() -> set[str]:
    """Every DOB the generator can emit, in raw HL7 and ISO forms."""
    dates: set[str] = set()
    for hosp in ("HOSPA", "HOSPB", "HOSPC"):
        for p in make_pool(hosp, N_PATIENTS):
            dates.add(p.birth_date)
            dates.add(f"{p.birth_date[0:4]}-{p.birth_date[4:6]}-{p.birth_date[6:8]}")
    return dates


def _store_values(db_path: Path) -> set[str]:
    """Identifiers and names actually held in the store: the ground truth."""
    conn = sqlite3.connect(db_path)
    try:
        values: set[str] = set()
        queries = (
            "SELECT mrn, family_name, given_name FROM patients",
            "SELECT accession, patient_mrn, placer_order_id FROM orders",
            "SELECT accession, patient_mrn FROM reports",
            "SELECT study_uid, accession, patient_mrn FROM studies",
            "SELECT control_id FROM messages",
        )
        for sql in queries:
            for row in conn.execute(sql):
                values.update(str(v) for v in row if v)
        return values
    finally:
        conn.close()


def find_leaks(text: str, extra: set[str] | None = None) -> list[str]:
    """Every canary found in ``text`` or in its URL-decoded form."""
    leaks: list[str] = []
    for view in (text, unquote(text)):
        for name in (*FAMILY_NAMES, *GIVEN_NAMES):
            if name in view:
                leaks.append(f"patient name {name!r}")
        leaks.extend(f"MRN {m!r}" for m in sorted(set(MRN_PATTERN.findall(view))))
        leaks.extend(f"accession {a!r}" for a in sorted(set(ACCESSION_PATTERN.findall(view))))
        for dob in sorted(_birth_dates()):
            if dob in view:
                leaks.append(f"birth date {dob!r}")
        for fragment in (*REPORT_LINES, IMPRESSION, CORRECTED_LINE, BAD_CLOCK_LINE):
            if fragment in view:
                leaks.append(f"report text {fragment!r}")
        for value in sorted({*CANARIES, *(extra or ())}):
            if value in view:
                leaks.append(f"value {value!r}")
    return sorted(set(leaks))


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("phi")
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    out = tmp / "synth"
    generate(out, registry, seed=42)
    return tmp, out, registry


@pytest.fixture(scope="module")
def loaded_db(corpus):
    """The corpus ingested and loaded, plus rows that sync must skip."""
    tmp, out, registry = corpus
    db_path = tmp / "loaded.db"
    conn = connect(db_path)
    pipeline = IngestPipeline(registry, conn)
    for path in sorted((out / "messages").rglob("*.hl7")):
        pipeline.handle_bytes(path.read_bytes())
    load_studies(conn, registry, out / "dicom")
    load_outbound(conn, out / "outbound_reports.jsonl")
    with conn:
        upsert_order(
            conn,
            OrderRow(
                hospital_id="HOSPA",
                accession=CANARY_ACC,
                placer_order_id=None,
                procedure_code="71250",
                procedure_description="CT CHEST",
                code_system="local",
                modality="CT",
                priority="R",
                patient_mrn=CANARY_MRN_ORPHAN,
                ordered_at_utc="2026-10-07T16:30:00+00:00",
                status="new",
            ),
        )
        insert_study(
            conn,
            StudyRow(
                study_uid=CANARY_STUDY_UID,
                hospital_id="HOSPA",
                accession=CANARY_ACC,
                modality="CT",
                patient_mrn=CANARY_MRN_ORPHAN,
                study_at_utc="2026-10-07T17:00:00+00:00",
                source_file="canary.dcm",
            ),
        )
    conn.close()
    values = _store_values(db_path)
    assert CANARY_ACC in values and CANARY_STUDY_UID in values
    return db_path, registry, values


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """main() installs a real handler; put the logging tree back afterwards."""
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    saved_levels = {n: logging.getLogger(n).level for n in THIRD_PARTY_HTTP_LOGGERS}
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


def ok_handler() -> Handler:
    """A FHIR server that answers every write: 201 the first time a URL is
    seen, 200 after that, so a second sync exercises the update path."""
    seen: set[str] = set()
    counter = iter(range(1, 1_000_000))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"resourceType": "CapabilityStatement"})
        if request.method == "GET":
            return httpx.Response(200, json={"resourceType": "Bundle", "total": 0})
        key = str(request.url)
        status = 200 if key in seen else 201
        seen.add(key)
        return httpx.Response(status, json={"id": str(next(counter))})

    return handler


def failing_handler(mode: str) -> Handler:
    """A FHIR server that fails the first write in a way that echoes the
    request, the way real servers do in OperationOutcome diagnostics."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"resourceType": "CapabilityStatement"})
        url = str(request.url)
        echo = f"{url} {unquote(url)} {CANARY_MRN} {CANARY_NAME}"
        if mode == "4xx_echo":
            return httpx.Response(
                400,
                json={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "invalid", "diagnostics": echo}],
                },
            )
        if mode == "412_multiple":
            return httpx.Response(412, text=f"Multiple resources match {echo}")
        if mode == "transport":
            raise httpx.ConnectError(f"connection refused while sending {echo}", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout(f"timed out waiting for {echo}", request=request)
        raise AssertionError(mode)

    return handler


def mock_client(handler: Handler) -> HapiClient:
    return HapiClient(FHIR_BASE, transport=httpx.MockTransport(handler))


# -- 1. ingest and loaders over the whole corpus -------------------------------


def test_no_phi_reaches_logs_ingest_and_loaders(corpus, caplog):
    tmp, out, registry = corpus
    db_path = tmp / "phi.db"
    conn = connect(db_path)
    pipeline = IngestPipeline(registry, conn)
    with caplog.at_level(logging.DEBUG):
        for path in sorted((out / "messages").rglob("*.hl7")):
            pipeline.handle_bytes(path.read_bytes())
        load_studies(conn, registry, out / "dicom")
        load_outbound(conn, out / "outbound_reports.jsonl")
    conn.close()

    log_text = caplog.text
    assert "message accepted" in log_text, "pipeline logging is off; test would be vacuous"
    assert "message rejected" in log_text, "quarantine logging is off; test would be vacuous"
    assert "unparseable message quarantined" in log_text
    assert "loaded studies" in log_text
    assert find_leaks(log_text, _store_values(db_path)) == []


# -- 2. FHIR sync through a mock transport ------------------------------------


def test_no_phi_in_fhir_sync_logs_create_update_skip(loaded_db, caplog):
    db_path, registry, values = loaded_db
    conn = connect(db_path)
    handler = ok_handler()
    # Root and httpx at DEBUG: the httpx request line must appear (proving
    # third-party records are captured) but with its query redacted.
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="httpx"):
        with mock_client(handler) as client:
            first = sync_to_fhir(conn, registry, client)
        with mock_client(handler) as client:
            second = sync_to_fhir(conn, registry, client)
    conn.close()

    assert sum(first.created.values()) > 0
    assert sum(second.updated.values()) > 0
    assert first.skipped, "the planted orphan rows must be skipped"
    # The keys are in the report (product output) ...
    assert any(CANARY_ACC in key for _, key, _ in first.skipped)

    log_text = caplog.text
    assert "HTTP Request: PUT" in log_text, "httpx logging not captured; test would be vacuous"
    assert "[query redacted]" in log_text
    assert "fhir sync skipped" in log_text
    assert "fhir sync finished" in log_text
    # ... and never in the log.
    assert find_leaks(log_text, values) == []


# -- 3. FHIR failure paths: client, sync and error text ------------------------


@pytest.mark.parametrize("mode", ["4xx_echo", "412_multiple", "transport", "timeout"])
def test_no_phi_in_fhir_failures(loaded_db, caplog, mode):
    db_path, registry, values = loaded_db
    conn = connect(db_path)
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="httpx"):
        with mock_client(failing_handler(mode)) as client, pytest.raises(FhirPostError) as ei:
            sync_to_fhir(conn, registry, client)
        # A caller that logs the failure message stays clean.
        logging.getLogger("hl7bridge.test").error("sync failed: %s", ei.value)
    conn.close()

    err = ei.value
    assert err.path == "/fhir/Patient"
    if mode == "4xx_echo":
        assert err.issue_codes == ["invalid"]
        assert "invalid" in str(err)
    # The body is kept for callers and tests, but never in the message.
    assert CANARY_MRN in err.body
    assert find_leaks(str(err), values) == []
    assert "sync failed" in caplog.text
    assert find_leaks(caplog.text, values) == []


def test_metadata_probe_failure_logs_no_url_query(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {CANARY_MRN}", request=request)

    with caplog.at_level(logging.DEBUG), mock_client(handler) as client:
        assert client.metadata_ok() is False
    assert "FHIR /metadata probe failed" in caplog.text
    assert "ConnectError" in caplog.text
    assert find_leaks(caplog.text) == []


# -- 4. hostile MSH-10 and MSH-9 ----------------------------------------------


def _orm(control_id: str, *, accession: str = "ACC123", orc1: str = "NW") -> bytes:
    msh = f"MSH|^~\\&|RIS|HOSPA|BRIDGE|HUB|20261007100000||ORM^O01|{control_id}|T|2.3"
    pid = f"PID|1||{CANARY_MRN}^^^HOSPA^MR||{CANARY_NAME}^ALPHA||19700101|F"
    orc = f"{orc1}|PL1|FIL1||||||20261007093000"
    obr = ["OBR", "1", "PL1", accession, "71250^CT CHEST^local"] + [""] * 20
    obr[24] = "CT"
    return ("\r".join([msh, pid, "ORC|" + orc, "|".join(obr)]) + "\r").encode()


def test_hostile_msh10_never_logged(tmp_path, caplog):
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    conn = connect(tmp_path / "hostile.db")
    pipeline = IngestPipeline(registry, conn)
    hostile_ctl = f"{CANARY_MRN}{CANARY_ACC}"
    with caplog.at_level(logging.DEBUG):
        accepted = pipeline.handle_bytes(_orm(hostile_ctl, accession=CANARY_ACC))
        duplicate = pipeline.handle_bytes(_orm(hostile_ctl, accession=CANARY_ACC))
        rejected = pipeline.handle_bytes(_orm(f"{hostile_ctl}X", orc1="XO"))
    conn.close()

    for ack, code in ((accepted, "AA"), (duplicate, "AA"), (rejected, "AE")):
        assert get_field(parse_hl7(ack), "MSA-1") == code
    log_text = caplog.text
    assert "message accepted" in log_text
    assert "duplicate message re-acked" in log_text
    assert "message rejected" in log_text
    # The log names the store row and a short hash, never the value.
    assert f"ctl_hash={short_hash(hostile_ctl)}" in log_text
    assert re.search(r"message_id=\d+", log_text)
    assert re.search(r"quarantine_id=\d+", log_text)
    assert find_leaks(log_text, {hostile_ctl}) == []


def test_hostile_ack_type_is_not_logged(tmp_path, caplog):
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    conn = connect(tmp_path / "ack.db")
    pipeline = IngestPipeline(registry, conn)
    ack_in = (
        f"MSH|^~\\&|EHR|HOSPA|BRIDGE|HUB|20261007100000||ACK^{CANARY_MRN}|A1|T|2.3\r"
        "MSA|AA|RPT0001\r"
    ).encode()
    with caplog.at_level(logging.DEBUG):
        pipeline.handle_bytes(ack_in)
    conn.close()
    assert "message accepted" in caplog.text
    assert "type=ACK" in caplog.text
    assert find_leaks(caplog.text) == []


# -- 5. parse errors -----------------------------------------------------------


def test_parse_error_logs_reason_code_and_length_only(tmp_path, caplog, monkeypatch):
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    conn = connect(tmp_path / "parse.db")
    pipeline = IngestPipeline(registry, conn)
    broken_segment = f"PID|1||{CANARY_MRN}^^^HOSPA^MR||{CANARY_NAME}^ALPHA".encode()
    not_utf8 = b"MSH|^~\\&|RIS|HOSPA|" + CANARY_MRN.encode() + b"\xff\xfe\r"

    def quoting_parser(text: str):
        # A parser whose error text quotes the input, as some do.
        raise HL7ParseError(f"cannot parse segment {text!r}")

    with caplog.at_level(logging.DEBUG):
        acks = [pipeline.handle_bytes(broken_segment), pipeline.handle_bytes(not_utf8)]
        monkeypatch.setattr(pipeline_module, "parse_hl7", quoting_parser)
        acks.append(pipeline.handle_bytes(_orm("C1")))
    conn.close()

    log_text = caplog.text
    assert log_text.count("unparseable message quarantined") == 3
    assert "cause=not_hl7" in log_text and "cause=not_utf8" in log_text
    assert find_leaks(log_text) == []
    # The ACK back to the sender carries the fixed reason text, not the parser's.
    for ack in acks:
        assert find_leaks(ack) == []
        assert "|AR|" in ack


# -- 6. internal errors --------------------------------------------------------


def test_internal_error_logs_type_and_frames_not_message(tmp_path, caplog):
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    conn = connect(tmp_path / "internal.db")
    pipeline = IngestPipeline(registry, conn)
    calls = {"n": 0}
    real_clock = pipeline.clock

    def flaky_clock():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"cannot store {CANARY_MRN} {CANARY_NAME} {CANARY_ACC}")
        return real_clock()

    pipeline.clock = flaky_clock
    with caplog.at_level(logging.DEBUG):
        ack = asyncio.run(pipeline.handle_frame(_orm("C1")))
    conn.close()

    assert b"|AR|" in ack
    log_text = caplog.text
    assert "internal error handling frame" in log_text
    assert "RuntimeError" in log_text
    assert "flaky_clock" in log_text, "stack frames must still be logged"
    assert find_leaks(log_text) == []


# -- 7. loader error text ------------------------------------------------------


def test_loader_errors_do_not_echo_field_values(tmp_path):
    conn = connect(tmp_path / "loader.db")
    bad = tmp_path / "outbound.jsonl"
    line = {
        "hospital_id": "HOSPA",
        "control_id": "R1",
        "accession": CANARY_ACC,
        "sent_at_utc": CANARY_MRN,
    }
    bad.write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(BridgeError) as ei:
        load_outbound(conn, bad)
    conn.close()
    assert "not an ISO 8601 timestamp" in str(ei.value)
    assert find_leaks(str(ei.value)) == []


def test_study_loader_error_does_not_echo_the_timestamp(corpus, tmp_path, monkeypatch):
    from hl7bridge.errors import TimestampError
    from hl7bridge.ingest import loaders

    _, out, registry = corpus

    def quoting_parser(value, **_kwargs):
        raise TimestampError(f"malformed HL7 timestamp: {CANARY_MRN!r} {value!r}")

    monkeypatch.setattr(loaders, "parse_hl7_ts", quoting_parser)
    conn = connect(tmp_path / "studies.db")
    with pytest.raises(loaders.StudyLoadError) as ei:
        load_studies(conn, registry, out / "dicom")
    conn.close()
    assert "not a valid timestamp" in str(ei.value)
    assert find_leaks(str(ei.value)) == []
    assert CANARY_MRN in str(ei.value.__cause__), "the cause keeps the detail for debugging"


# -- 8. the CLI with its real logging setup ------------------------------------


@pytest.fixture
def cli_db(loaded_db):
    db_path, _, values = loaded_db
    return db_path, values


def _patch_client(monkeypatch, handler: Handler) -> None:
    def factory(base_url: str, **kwargs):
        return HapiClient(base_url, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli, "HapiClient", factory)


def _run_cli(argv: list[str]) -> int:
    return cli.main(argv)


@pytest.mark.usefixtures("restore_logging")
def test_cli_fhir_sync_stderr_has_no_phi(cli_db, capsys, monkeypatch):
    db_path, values = cli_db
    _patch_client(monkeypatch, ok_handler())
    rc = _run_cli(
        ["fhir-sync", "--db", str(db_path), "--config", str(CONFIG_DIR), "--fhir-base", FHIR_BASE]
    )
    captured = capsys.readouterr()
    assert rc == 1, "the planted orphan rows are skipped, so the sync reports failure"
    assert "fhir sync finished" in captured.err, "CLI logging is off; test would be vacuous"
    assert "fhir sync skipped" in captured.err
    # httpx is held at WARNING by the CLI, so no request lines at all.
    assert "HTTP Request" not in captured.err
    assert find_leaks(captured.err, values) == []
    # Stdout is the report and does name the skipped keys (D16).
    assert CANARY_ACC in captured.out


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("mode", ["4xx_echo", "412_multiple", "transport", "timeout"])
def test_cli_fhir_sync_failure_stderr_has_no_phi(cli_db, capsys, monkeypatch, mode):
    db_path, values = cli_db
    _patch_client(monkeypatch, failing_handler(mode))
    rc = _run_cli(
        ["fhir-sync", "--db", str(db_path), "--config", str(CONFIG_DIR), "--fhir-base", FHIR_BASE]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "FHIR error:" in captured.err
    assert "/fhir/Patient" in captured.err
    assert find_leaks(captured.err, values) == []
    assert find_leaks(captured.out, values) == []


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("status", [401, 500])
def test_cli_fhir_probe_failure_has_no_phi(capsys, monkeypatch, status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"denied for {CANARY_MRN} {CANARY_NAME}")

    _patch_client(monkeypatch, handler)
    rc = _run_cli(["fhir-probe", "--fhir-base", FHIR_BASE])
    captured = capsys.readouterr()
    assert rc == 1
    assert f"FHIR server returned {status}" in captured.err
    assert find_leaks(captured.err) == []
    assert find_leaks(captured.out) == []


@pytest.mark.usefixtures("restore_logging")
def test_cli_fhir_probe_transport_failure_has_no_phi(capsys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.url} {CANARY_MRN}", request=request)

    _patch_client(monkeypatch, handler)
    rc = _run_cli(["fhir-probe", "--fhir-base", FHIR_BASE])
    captured = capsys.readouterr()
    assert rc == 1
    assert "transport error (ConnectError)" in captured.err
    assert find_leaks(captured.err) == []


# -- 9. the redaction layer itself ---------------------------------------------


def test_redact_url_queries():
    url = f"http://h/fhir/Patient?identifier=urn%3Ax%7C{CANARY_MRN}&_count=1"
    expected = "PUT http://h/fhir/Patient?[query redacted] done"
    assert redact_url_queries(f"PUT {url} done") == expected
    assert redact_url_queries(expected) == expected, "redaction must be idempotent"
    assert redact_url_queries("no url here") == "no url here"


@pytest.mark.usefixtures("restore_logging")
def test_debug_level_cannot_bring_the_url_leak_back(capsys):
    """Even if someone turns httpx back up to DEBUG after the CLI setup, the
    filters still strip the query from every request line."""
    configure_logging(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    with mock_client(ok_handler()) as client:
        client.search_total("Patient", {"identifier": f"urn:x|{CANARY_MRN}"})
    err = capsys.readouterr().err
    assert "HTTP Request: GET" in err
    assert "[query redacted]" in err
    assert find_leaks(err) == []


def test_redactor_handles_records_with_args():
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "GET %s", (f"http://h/p?id={CANARY_MRN}",), None
    )
    assert UrlQueryRedactor().filter(record)
    assert record.getMessage() == "GET http://h/p?[query redacted]"
