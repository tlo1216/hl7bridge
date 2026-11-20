"""No PHI in logs or error text.

Every test here captures the ROOT logger at DEBUG, so third-party loggers
(httpx, httpcore) are included, then scans the captured text for canaries:

- every patient name, MRN, birth date, accession, study UID, control id and
  report phrase the synthetic corpus holds, read back from the store, plus
- hand-made canaries planted in hostile places (MSH-10, MSH-9, a broken
  segment, an exception message, a FHIR server body, a transport error, an
  HTTP reason phrase and Location header from a real socket server, a
  pydicom validation message, an outbound log line).

Error text is checked as the full formatted traceback
(``traceback.format_exception``), so a chained cause cannot hide a value,
and the CLI is checked through ``main()`` with its real logging setup.

An autouse fixture strips the hl7bridge filters from the HTTP client
loggers before each test and restores the logging tree after it, so no test
passes because an earlier one left a filter in place.

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
import sys
import threading
import traceback
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import httpx
import pydicom
import pytest
from fhir.resources.R4B.patient import Patient

from hl7bridge import cli
from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import BridgeError, FhirPostError, HL7ParseError, TimestampError
from hl7bridge.fhir import sync_to_fhir
from hl7bridge.fhir.client import HapiClient
from hl7bridge.ingest import IngestPipeline
from hl7bridge.ingest import loaders as loaders_module
from hl7bridge.ingest import pipeline as pipeline_module
from hl7bridge.ingest.loaders import load_outbound, load_studies
from hl7bridge.ingest.parser import get_field, parse_hl7
from hl7bridge.logsafety import (
    QUIET_LOGGERS,
    LogRedactor,
    configure_logging,
    http_logger_names,
    redact_url_queries,
    safe_url,
)
from hl7bridge.profiles import BUNDLED_PROFILES
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

from ..fakefhir import FakeFhir

# Fake credentials, joined at runtime so no literal user:password URL sits in the source.
_CRED = "alice" + ":" + "s3cr3t"
_SHORT_CRED = "user" + ":" + "pw"
_TOKEN = "s3cr3t" + "-token"


CONFIG_DIR = BUNDLED_PROFILES
REGISTRY = HospitalRegistry.load_dir(CONFIG_DIR)
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


def traceback_text(exc: BaseException) -> str:
    """Everything Python would print for ``exc``, chained causes included."""
    return "".join(traceback.format_exception(exc))


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
    load_outbound(conn, registry, out / "outbound_reports.jsonl")
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


@pytest.fixture(autouse=True)
def isolated_logging() -> Iterator[None]:
    """Start each test with no hl7bridge filter on any HTTP client logger
    (so a filter left by an earlier test cannot make this one pass), and put
    the whole logging tree back afterwards (main() installs a real handler)."""
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    names = sorted({*http_logger_names(), *QUIET_LOGGERS})
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).filters)) for n in names}
    for name in names:
        logger = logging.getLogger(name)
        for f in list(logger.filters):
            if isinstance(f, LogRedactor):
                logger.removeFilter(f)
        logger.setLevel(logging.NOTSET)
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)
    for name, (level, filters) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.filters[:] = filters


@pytest.fixture
def restore_logging(isolated_logging) -> Iterator[None]:
    """Kept for readability at the CLI tests: the autouse fixture does the work."""
    yield


def ok_handler() -> Handler:
    """A FHIR server that answers every write (the in-memory one): created
    the first time, updated after that, so a second sync exercises the
    update path."""
    return FakeFhir()


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
        load_outbound(conn, registry, out / "outbound_reports.jsonl")
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
    assert any(CANARY_STUDY_UID in key for _, key, _ in first.skipped)

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
    # The log names the store row, never the value, not even as a hash.
    assert "ctl_hash" not in log_text
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
        load_outbound(conn, REGISTRY, bad)
    conn.close()
    assert "not an ISO 8601 timestamp" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert find_leaks(traceback_text(ei.value)) == []


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
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert find_leaks(traceback_text(ei.value)) == []


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
        [
            "-v",
            "fhir",
            "sync",
            "--db",
            str(db_path),
            "--config",
            str(CONFIG_DIR),
            "--fhir-base",
            FHIR_BASE,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1, "the planted orphan rows are skipped, so the sync reports failure"
    assert "fhir sync finished" in captured.err, "CLI logging is off; test would be vacuous"
    assert "fhir sync skipped" in captured.err
    # httpx is held at WARNING by the CLI, so no request lines at all.
    assert "HTTP Request" not in captured.err
    assert find_leaks(captured.err, values) == []
    # Stdout is the report and does name the skipped keys (D16).
    assert CANARY_STUDY_UID in captured.out


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("mode", ["4xx_echo", "412_multiple", "transport", "timeout"])
def test_cli_fhir_sync_failure_stderr_has_no_phi(cli_db, capsys, monkeypatch, mode):
    db_path, values = cli_db
    _patch_client(monkeypatch, failing_handler(mode))
    rc = _run_cli(
        [
            "fhir",
            "sync",
            "--db",
            str(db_path),
            "--config",
            str(CONFIG_DIR),
            "--fhir-base",
            FHIR_BASE,
        ]
    )
    captured = capsys.readouterr()
    # A server that answered with an error is a finding; one that never
    # answered is a connection error.
    assert rc == (3 if mode in ("transport", "timeout") else 1)
    assert "error: FHIR" in captured.err
    assert "Traceback" not in captured.err
    assert "/fhir/Patient" in captured.err
    assert find_leaks(captured.err, values) == []
    assert find_leaks(captured.out, values) == []


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("status", [401, 500])
def test_cli_fhir_probe_failure_has_no_phi(capsys, monkeypatch, status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"denied for {CANARY_MRN} {CANARY_NAME}")

    _patch_client(monkeypatch, handler)
    rc = _run_cli(["fhir", "probe", "--fhir-base", FHIR_BASE])
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
    rc = _run_cli(["fhir", "probe", "--fhir-base", FHIR_BASE])
    captured = capsys.readouterr()
    assert rc == 3
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
    assert LogRedactor().filter(record)
    assert record.getMessage() == "GET http://h/p?[query redacted]"


# -- 10. real httpcore over a local socket -------------------------------------


@contextmanager
def echoing_fhir_server() -> Iterator[str]:
    """A real HTTP server on 127.0.0.1 that answers every request with a
    reason phrase, a Location query and a header that all hold canaries,
    the way a server or proxy echoing the request might. httpx goes through
    httpcore here, unlike MockTransport."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps({"resourceType": "Patient", "id": "1", "total": 0}).encode()
            status = 201 if self.command == "PUT" else 200
            self.send_response(status, f"Created for {CANARY_MRN} {CANARY_NAME}")
            self.send_header(
                "Location", f"/fhir/Patient/1/_history/1?identifier=urn:x|{CANARY_MRN}"
            )
            self.send_header("X-Echo", f"{CANARY_NAME} {CANARY_ACC}")
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_PUT = _answer
        do_GET = _answer

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep the test server quiet

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/fhir"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.fixture
def no_proxy(monkeypatch):
    """The local server must be reached directly, never through a proxy."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def _canary_patient() -> Patient:
    return Patient(identifier=[{"system": "urn:x", "value": CANARY_MRN}])


def _exercise_real_transport(base: str) -> None:
    with HapiClient(base) as client:
        assert client.upsert(_canary_patient()) == ("1", "created")
        client.search_total("Patient", {"identifier": f"urn:x|{CANARY_MRN}"})


@pytest.mark.usefixtures("no_proxy")
def test_real_httpcore_at_debug_library_use(caplog):
    """A library user with root at DEBUG and their own handler (caplog's).
    Only the logger-level filters the client installs stand in the way."""
    with (
        echoing_fhir_server() as base,
        caplog.at_level(logging.DEBUG),
        caplog.at_level(logging.DEBUG, logger="httpx"),
        caplog.at_level(logging.DEBUG, logger="httpcore"),
    ):
        _exercise_real_transport(base)
    log_text = caplog.text
    assert "receive_response_headers.complete" in log_text, "httpcore not captured"
    assert "connect_tcp.started" in log_text
    assert "HTTP Request: PUT" in log_text
    assert '"HTTP/1.1 201"' in log_text
    assert find_leaks(log_text) == []


@pytest.mark.usefixtures("no_proxy")
def test_real_httpcore_at_debug_through_the_cli_handler(capsys):
    """The CLI handler alone, with the logger-level filters removed again
    after configure_logging, still emits nothing outside the safe fields."""
    configure_logging(logging.DEBUG)
    for name in http_logger_names():
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        for f in list(logger.filters):
            logger.removeFilter(f)
    with echoing_fhir_server() as base:
        # Construct the client by hand so it cannot reinstall the filters.
        client = HapiClient.__new__(HapiClient)
        client.base_url = base
        client._client = httpx.Client(headers={"Accept": "application/fhir+json"})
        with client:
            client.upsert(_canary_patient())
    err = capsys.readouterr().err
    assert "httpcore.http11: receive_response_headers.complete HTTP/1.1 201" in err
    assert "HTTP Request: PUT" in err
    assert find_leaks(err) == []


def test_reason_phrase_is_not_logged(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": "1"},
            extensions={"reason_phrase": f"Created {CANARY_MRN}".encode()},
        )

    with (
        caplog.at_level(logging.DEBUG),
        caplog.at_level(logging.DEBUG, logger="httpx"),
        mock_client(handler) as client,
    ):
        client.upsert(_canary_patient())
    assert "HTTP Request: PUT" in caplog.text
    assert find_leaks(caplog.text) == []


def test_unknown_http_record_is_withheld(caplog):
    install_target = logging.getLogger("httpcore.http11")
    with mock_client(ok_handler()):
        pass
    with caplog.at_level(logging.DEBUG):
        install_target.debug("some.new_event headers=%s", [(b"X", CANARY_MRN.encode())])
        logging.getLogger("httpx").info("brand new format %s", CANARY_NAME)
    assert "record withheld" in caplog.text
    assert find_leaks(caplog.text) == []


# -- 11. the handler filter and the traceback pre-render -------------------------


@pytest.mark.usefixtures("restore_logging")
def test_cli_handler_redacts_any_logger(capsys):
    """A logger nobody listed: only the handler filter can catch it."""
    configure_logging(logging.DEBUG)
    logging.getLogger("someone.else").warning(
        "fetched http://" + _SHORT_CRED + "@h/fhir/Patient?identifier=x|%s and /fhir/Patient?id=%s",
        CANARY_MRN,
        CANARY_ACC,
    )
    err = capsys.readouterr().err
    assert "[query redacted]" in err
    assert "[user-info-redacted]" in err
    assert "pw@" not in err
    assert find_leaks(err) == []


@pytest.mark.usefixtures("restore_logging")
def test_cli_handler_redacts_traceback_text(capsys):
    configure_logging(logging.DEBUG)
    try:
        raise RuntimeError(f"failed GET http://h/fhir/Patient?identifier=x|{CANARY_MRN}")
    except RuntimeError:
        logging.getLogger("someone.else").exception("request failed")
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "[query redacted]" in err
    assert find_leaks(err) == []


def test_redactor_renders_a_fresh_traceback_redacted():
    """No other handler has formatted this record first, so exc_text is
    empty and the filter must render it from exc_info itself."""
    try:
        raise RuntimeError(f"GET http://h/fhir/Patient?identifier=x|{CANARY_MRN}")
    except RuntimeError:
        record = logging.getLogger("someone.else").makeRecord(
            "someone.else", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    assert LogRedactor().filter(record)
    text = logging.Formatter().format(record)
    assert "RuntimeError" in text and "[query redacted]" in text
    assert find_leaks(text) == []


def test_redact_relative_targets_and_user_info():
    assert (
        redact_url_queries(f"Location: /fhir/Patient/1?identifier=x|{CANARY_MRN} ok")
        == "Location: /fhir/Patient/1?[query redacted] ok"
    )
    assert (
        redact_url_queries(f"(b'Location', b'/fhir/Patient?identifier={CANARY_MRN}')")
        == "(b'Location', b'/fhir/Patient?[query redacted]')"
    )
    assert (
        redact_url_queries("https://" + _SHORT_CRED + "@h/p") == "https://[user-info-redacted]@h/p"
    )
    assert redact_url_queries("a/b?c and 3/4?") == "a/b?c and 3/4?"
    assert safe_url("https://" + _SHORT_CRED + "@h:8443/fhir/Patient?x=1#f") == (
        "https://h:8443/fhir/Patient?[query redacted]"
    )


# -- 12. exception chains: the whole traceback ----------------------------------


@pytest.mark.parametrize("mode", ["transport", "timeout"])
def test_fhir_transport_error_traceback_has_no_phi(mode):
    with mock_client(failing_handler(mode)) as client, pytest.raises(FhirPostError) as ei:
        client.upsert(_canary_patient())
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    text = traceback_text(ei.value)
    assert "transport error" in text
    assert find_leaks(text) == []


def test_capability_probe_transport_error_traceback_has_no_phi():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.url} {CANARY_MRN}", request=request)

    with mock_client(handler) as client, pytest.raises(FhirPostError) as ei:
        client.capability_summary()
    assert find_leaks(traceback_text(ei.value)) == []


def test_logger_exception_on_fhir_error_has_no_phi(caplog):
    with caplog.at_level(logging.DEBUG), mock_client(failing_handler("transport")) as client:
        try:
            client.upsert(_canary_patient())
        except FhirPostError:
            logging.getLogger("hl7bridge.test").exception("sync failed")
    assert "FhirPostError" in caplog.text
    assert find_leaks(caplog.text) == []


# -- 13. pydicom validation output: logging and warnings ------------------------


def _write_bad_dicom(corpus_out: Path, dest: Path, **values: str) -> None:
    """Copy one generated study and overwrite elements with invalid values."""
    source = sorted((corpus_out / "dicom" / "HOSPA").glob("*.dcm"))[0]
    folder = dest / "HOSPA"
    folder.mkdir(parents=True)
    # Writing the bad values makes pydicom complain too; keep that out of
    # the capture, which is for the loader only.
    pydicom_logger = logging.getLogger("pydicom")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ds = pydicom.dcmread(source)
        saved = pydicom.config.settings.writing_validation_mode
        pydicom.config.settings.writing_validation_mode = pydicom.config.IGNORE
        pydicom_logger.disabled = True
        try:
            for keyword, value in values.items():
                setattr(ds, keyword, value)
            ds.save_as(folder / "bad.dcm")
        finally:
            pydicom.config.settings.writing_validation_mode = saved
            pydicom_logger.disabled = False


BAD_DICOM_VALUES = {
    "uid": {"StudyInstanceUID": f"1.2.{CANARY_MRN}.{CANARY_NAME}"},
    "time": {"StudyTime": f"99{CANARY_MRN}"},
}


@pytest.mark.parametrize("case", sorted(BAD_DICOM_VALUES))
def test_pydicom_validation_is_captured_on_both_channels(corpus, tmp_path, caplog, case):
    _, out, registry = corpus
    dicom_root = tmp_path / "dicom"
    _write_bad_dicom(out, dicom_root, **BAD_DICOM_VALUES[case])
    conn = connect(tmp_path / "studies.db")
    with (
        caplog.at_level(logging.DEBUG),
        warnings.catch_warnings(record=True) as seen,
    ):
        warnings.simplefilter("always")
        with pytest.raises(loaders_module.StudyLoadError) as ei:
            load_studies(conn, registry, dicom_root)
    conn.close()
    assert "invalid" in str(ei.value) or "not a valid timestamp" in str(ei.value)
    assert find_leaks(traceback_text(ei.value)) == []
    assert find_leaks(caplog.text) == []
    assert find_leaks(" ".join(str(w.message) for w in seen)) == []
    # Nothing escaped the capture on either channel.
    assert [r for r in caplog.records if r.name.startswith("pydicom")] == []
    assert [w for w in seen if "pydicom" in str(w.filename)] == []


# -- 14. the CLI error boundary --------------------------------------------------


def _outbound_file(tmp_path: Path, **overrides: str) -> Path:
    line = {
        "hospital_id": "HOSPA",
        "control_id": "R1",
        "accession": "ACC1",
        "sent_at_utc": "2026-10-07T12:00:00+00:00",
        **overrides,
    }
    path = tmp_path / "outbound.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return path


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(
    "overrides",
    [
        {"sent_at_utc": f"{CANARY_MRN} {CANARY_NAME} 1970-01-01"},
        {"hospital_id": f"HOSPZ {CANARY_MRN}"},
        {"hospital_id": f"HOSPZ {CANARY_MRN}", "control_id": f"{CANARY_MRN}-CTL"},
    ],
    ids=["bad_timestamp", "unknown_hospital", "unknown_hospital_and_hostile_msh10"],
)
def test_cli_load_outbound_errors_have_no_phi(tmp_path, capsys, overrides):
    path = _outbound_file(tmp_path, **overrides)
    rc = _run_cli(
        [
            "load",
            "outbound",
            "--db",
            str(tmp_path / "o.db"),
            "--config",
            str(CONFIG_DIR),
            "--path",
            str(path),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "error:" in captured.err
    assert "Traceback" not in captured.err
    assert find_leaks(captured.err) == []
    assert find_leaks(captured.out) == []


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("case", sorted(BAD_DICOM_VALUES))
def test_cli_load_studies_errors_have_no_phi(corpus, tmp_path, capsys, case):
    _, out, _ = corpus
    dicom_root = tmp_path / "dicom"
    _write_bad_dicom(out, dicom_root, **BAD_DICOM_VALUES[case])
    rc = _run_cli(
        [
            "load",
            "studies",
            "--db",
            str(tmp_path / "s.db"),
            "--config",
            str(CONFIG_DIR),
            "--dicom",
            str(dicom_root),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "error:" in captured.err
    assert "Traceback" not in captured.err
    assert find_leaks(captured.err) == []
    assert "bad.dcm" not in captured.err, "file names can be study UIDs"


@pytest.mark.usefixtures("restore_logging")
def test_cli_ack_report_with_unconfigured_hospital_has_no_phi(tmp_path, capsys):
    db = tmp_path / "a.db"
    conn = connect(db)
    with conn:
        conn.execute(
            "INSERT INTO outbound_reports (hospital_id, control_id, accession, sent_at_utc)"
            " VALUES (?, ?, ?, ?)",
            (f"HOSPZ{CANARY_NAME}", f"{CANARY_MRN}-CTL", CANARY_ACC, "2026-10-07T12:00:00+00:00"),
        )
    conn.close()
    rc = _run_cli(
        [
            "ack-report",
            "--db",
            str(db),
            "--config",
            str(CONFIG_DIR),
            "--now",
            "2026-10-08T12:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "UNCONFIGURED HOSPITAL: 1 outbound report(s)" in captured.out
    assert find_leaks(captured.out) == []
    assert find_leaks(captured.err) == []


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"cannot store {CANARY_MRN} {CANARY_NAME}"),
        TimestampError(f"malformed HL7 timestamp: {CANARY_MRN!r}"),
        HL7ParseError(f"cannot parse {CANARY_NAME}"),
    ],
    ids=["foreign", "timestamp", "parse"],
)
@pytest.mark.parametrize("debug", [False, True], ids=["plain", "debug"])
def test_cli_boundary_withholds_messages_that_quote_input(
    tmp_path, capsys, monkeypatch, error, debug
):
    def boom(_conn):
        try:
            raise ValueError(f"inner {CANARY_ACC}")
        except ValueError as inner:
            raise error from inner

    monkeypatch.setattr(cli, "message_counts", boom)
    connect(tmp_path / "x.db").close()  # report commands open an existing database only
    argv = ["summary", "--db", str(tmp_path / "x.db")]
    rc = _run_cli(["--debug", *argv] if debug else argv)
    captured = capsys.readouterr()
    assert rc == (4 if isinstance(error, RuntimeError) else 1)
    assert type(error).__name__ in captured.err
    assert "message withheld" in captured.err
    if debug:
        assert "boom" in captured.err, "--debug prints the stack frames"
    else:
        assert "boom" not in captured.err, "no stack frames without --debug"
        assert "--debug" in captured.err
    assert "Traceback" not in captured.err
    assert find_leaks(captured.err) == []


@pytest.mark.usefixtures("restore_logging")
def test_cli_credentials_in_fhir_base_are_never_printed(tmp_path, capsys, monkeypatch):
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="denied")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.url}", request=request)

    secret_base = "http://demo-user:" + _TOKEN + "@fhir.test/fhir"
    connect(tmp_path / "c.db").close()
    _patch_client(monkeypatch, denied)
    rc_probe = _run_cli(["fhir", "probe", "--fhir-base", secret_base])
    _patch_client(monkeypatch, refused)
    rc_sync = _run_cli(
        [
            "fhir",
            "sync",
            "--db",
            str(tmp_path / "c.db"),
            "--config",
            str(CONFIG_DIR),
            "--fhir-base",
            secret_base,
        ]
    )
    rc_doctor = _run_cli(["doctor", "--fhir-base", secret_base, "--db", str(tmp_path / "c.db")])
    rc_doctor_json = _run_cli(["doctor", "--json", "--fhir-base", secret_base])
    captured = capsys.readouterr()
    assert rc_probe == 1
    assert rc_sync == 3
    assert rc_doctor == rc_doctor_json == 1
    assert "[FAIL] fhir: no answer from http://fhir.test/fhir/metadata" in captured.out
    assert '"detail": "no answer from http://fhir.test/fhir/metadata"' in captured.out
    everything = captured.out + captured.err
    assert "server: http://fhir.test/fhir" in captured.out
    assert "FHIR /metadata probe failed for http://fhir.test/fhir" in captured.err
    assert "error: no answer from http://fhir.test/fhir/metadata" in captured.err
    assert "s3cr3t" not in everything and "demo-user" not in everything


# -- 15. ACK text back to the sender ---------------------------------------------


def _adt(control_id: str, *, pid7: str = "19700101", msh4: str = "HOSPA") -> bytes:
    msh = f"MSH|^~\\&|RIS|{msh4}|BRIDGE|HUB|20261007100000||ADT^A04|{control_id}|T|2.3"
    pid = f"PID|1||{CANARY_MRN}^^^HOSPA^MR||{CANARY_NAME}^ALPHA||{pid7}|F"
    return ("\r".join([msh, pid]) + "\r").encode()


ACK_CASES = {
    "bad_birth_date": (_adt("C1", pid7=f"1970-{CANARY_MRN}"), "AE", "bad_timestamp: PID-7"),
    "unknown_hospital": (_adt("C2", msh4=f"X{CANARY_NAME}"), "AR", "unknown_hospital: MSH-4/MSH-6"),
    "order_control": (_orm("C3", orc1="XQ"), "AE", "unsupported_order_control: ORC-1"),
    "unknown_type": (
        _orm("C4").replace(b"ORM^O01", f"ORM^{CANARY_MRN}".encode()),
        "AR",
        "unexpected_message_type: MSH-9",
    ),
}


@pytest.mark.parametrize("case", sorted(ACK_CASES))
def test_ack_text_carries_reason_and_field_only(tmp_path, caplog, case):
    payload, code, text = ACK_CASES[case]
    conn = connect(tmp_path / "ack.db")
    pipeline = IngestPipeline(REGISTRY, conn)
    with caplog.at_level(logging.DEBUG):
        ack = pipeline.handle_bytes(payload)
    # Every rejection is logged, routing-stage ones included, without values.
    assert "message rejected" in caplog.text
    assert find_leaks(caplog.text, {"XQ"}) == []
    detail = conn.execute("SELECT detail FROM quarantine").fetchone()["detail"]
    conn.close()
    message = parse_hl7(ack)
    assert get_field(message, "MSA-1") == code
    assert get_field(message, "MSA-3") == text
    assert find_leaks(ack.split("\r", 1)[1], {"Z" + CANARY_NAME[:1]}) == []
    # The quarantine row keeps the detail for whoever reviews it.
    assert detail


# -- 16. the listener boundary when the store fails too --------------------------


def test_listener_boundary_survives_a_failing_quarantine(tmp_path, caplog):
    conn = connect(tmp_path / "boundary.db")
    pipeline = IngestPipeline(REGISTRY, conn)

    def exploding(_raw: bytes) -> str:
        raise KeyError(f"{CANARY_MRN} {CANARY_NAME}")

    def no_store(*_args, **_kwargs) -> int:
        raise sqlite3.OperationalError(f"database is locked while writing {CANARY_ACC}")

    pipeline.handle_bytes = exploding  # type: ignore[method-assign]
    pipeline._quarantine = no_store  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        ack = asyncio.run(pipeline.handle_frame(_orm("C1")))
    conn.close()
    assert b"|AR|" in ack
    assert "internal error handling frame quarantine_id=none" in caplog.text
    assert "quarantine write failed error=OperationalError" in caplog.text
    assert find_leaks(caplog.text) == []
    assert find_leaks(ack.decode()) == []


# -- 17. OperationOutcome issue codes ----------------------------------------------


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        ([{"code": "invalid"}, {"code": "not-found"}], ["invalid", "not-found"]),
        ([{"code": "zzzcanary-alpha"}], ["unrecognized-code"]),
        ([{"code": CANARY_NAME.lower()}, {"code": "invalid"}], ["unrecognized-code", "invalid"]),
        ([{"code": 5}, "junk", None], ["unrecognized-code"]),
        (5, []),
        ("invalid", []),
        ({"code": "invalid"}, []),
        (None, []),
    ],
)
def test_issue_codes_are_from_the_value_set_only(issue, expected):
    body = json.dumps({"resourceType": "OperationOutcome", "issue": issue})
    err = FhirPostError(400, "http://h/fhir/Patient?identifier=x", body)
    assert err.issue_codes == expected
    assert find_leaks(str(err)) == []
    assert CANARY_NAME.lower() not in str(err)


# -- 18. the 0.3 commands: demo, send, doctor ---------------------------------------


@pytest.mark.usefixtures("restore_logging")
def test_cli_demo_logs_and_errors_have_no_phi(tmp_path, capsys, monkeypatch):
    """The whole demo at DEBUG through main(): every log line on stderr is
    scanned. Stdout holds the reports and is not checked (D16)."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    rc = _run_cli(["--debug", "demo"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out[-2000:]
    assert "message rejected" in captured.err, "debug logging is off; test would be vacuous"
    assert "mllp connection opened" in captured.err
    assert find_leaks(captured.err) == []


@pytest.mark.usefixtures("restore_logging")
def test_cli_send_stderr_names_no_message_values(tmp_path, capsys):
    """send puts per-file ACK details on stdout; stderr carries only the
    status lines and the error, never a file name or a field value."""
    msgs = tmp_path / "msgs"
    msgs.mkdir()
    (msgs / f"{CANARY_MRN}.hl7").write_bytes(_adt(f"{CANARY_MRN}-CTL"))
    with socket_closed_port() as port:
        rc = _run_cli(["send", str(msgs), "--port", str(port)])
    captured = capsys.readouterr()
    assert rc == 3
    assert "error: cannot connect" in captured.err
    assert CANARY_MRN not in captured.err.replace(str(msgs), "")
    assert find_leaks(captured.err.replace(str(msgs), "")) == []


@contextmanager
def socket_closed_port() -> Iterator[int]:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    yield port


@pytest.mark.parametrize(
    "url",
    [
        "alice:s3cr3t@127.0.0.1:9/fhir",
        "http:/alice:s3cr3t@host/fhir",
        "https:alice:s3cr3t@host/fhir",
        "http:///alice:s3cr3t@host/fhir",
        "//alice:s3cr3t@host/fhir",
        "http://" + _CRED + "@host/fhir",
        "http://" + _CRED + "@host:notaport/fhir",
        "alice@s3cr3t-host/fhir",
    ],
)
def test_safe_url_and_error_paths_never_return_user_info(url):
    """Even a mistyped URL: urlsplit then puts the password in the path."""
    from hl7bridge.errors import _url_path

    for shown in (safe_url(url), _url_path(url), str(FhirPostError(0, url))):
        assert "s3cr3t" not in shown and "alice" not in shown, shown


# -- 9. the 0.4 message commands: to-fhir, explain, replay ----------------------


@pytest.mark.usefixtures("restore_logging")
def test_cli_to_fhir_over_the_corpus_has_no_phi_in_errors_or_logs(corpus, tmp_path, capsys):
    """The corpus holds seeded defects, so mapping errors are printed; none
    may quote a value. Bundles go to files, which are product output."""
    _, out, _ = corpus
    rc = _run_cli(
        [
            "-vv",
            "to-fhir",
            str(out / "messages"),
            "--config",
            str(CONFIG_DIR),
            "--out",
            str(tmp_path / "bundles"),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "error: " in captured.err, "no mapping error printed; test would be vacuous"
    assert "could not be mapped" in captured.err
    assert find_leaks(captured.err) == []
    assert find_leaks(captured.out) == []


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("mode", ["4xx_echo", "transport", "timeout"])
def test_cli_to_fhir_post_failure_has_no_phi(tmp_path, capsys, monkeypatch, mode):
    _patch_client(monkeypatch, failing_handler(mode))
    message = tmp_path / "a.hl7"
    message.write_bytes(
        (
            "MSH|^~\\&|ADT|HOSPA|HL7BRIDGE|HUB|20261007080000||ADT^A04|C1|T|2.3\r"
            f"PID|1||{CANARY_MRN}^^^HOSPA^MR||{CANARY_NAME}^ALPHA||19800101|F\r"
        ).encode()
    )
    rc = _run_cli(["-vv", "to-fhir", str(message), "--post", "--fhir-base", FHIR_BASE])
    captured = capsys.readouterr()
    assert rc == (3 if mode in ("transport", "timeout") else 1)
    assert "error: FHIR" in captured.err
    assert find_leaks(captured.err) == []
    assert find_leaks(captured.out) == []


@pytest.mark.usefixtures("restore_logging")
def test_cli_explain_writes_values_to_stdout_only(corpus, capsys, caplog):
    _, out, _ = corpus
    first = sorted((out / "messages").rglob("*.hl7"))[0]
    with caplog.at_level(logging.DEBUG):
        rc = _run_cli(["-vv", "explain", str(first)])
    captured = capsys.readouterr()
    assert rc == 0
    assert find_leaks(captured.out) != [], "explain prints the message; test would be vacuous"
    assert find_leaks(captured.err) == []
    assert find_leaks(caplog.text) == []


@pytest.mark.usefixtures("restore_logging")
def test_cli_replay_dry_run_prints_no_identifiers(loaded_db, capsys):
    db_path, _, values = loaded_db
    rc = _run_cli(["replay", "--db", str(db_path), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "would be sent" in captured.out, "nothing listed; test would be vacuous"
    assert find_leaks(captured.out, values) == []
    assert find_leaks(captured.err, values) == []


def test_sync_skip_reasons_for_unmapped_codes_hold_no_value(loaded_db, caplog):
    db_path, _, values = loaded_db
    canary = "QX7"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # sync maps the stored messages, so the canary goes into one of them.
    saved = conn.execute(
        "SELECT id, raw FROM messages WHERE message_type = 'ADT^A04' ORDER BY id LIMIT 1"
    ).fetchone()
    pid = next(seg for seg in saved["raw"].split("\r") if seg.startswith("PID|"))
    fields = pid.split("|")
    fields[8] = canary
    conn.execute(
        "UPDATE messages SET raw = ? WHERE id = ?",
        (saved["raw"].replace(pid, "|".join(fields)), saved["id"]),
    )
    conn.commit()
    try:
        registry = HospitalRegistry.load_dir(CONFIG_DIR)
        with caplog.at_level(logging.DEBUG), mock_client(ok_handler()) as client:
            report = sync_to_fhir(conn, registry, client)
    finally:
        conn.execute("UPDATE messages SET raw = ? WHERE id = ?", (saved["raw"], saved["id"]))
        conn.commit()
        conn.close()
    reasons = [reason for kind, _, reason in report.skipped if kind == "ADT^A04"]
    assert reasons == ["PID-8: code not in table 0001 (administrative sex); refusing to guess"]
    assert "fhir sync skipped 1 ADT^A04 row(s)" in caplog.text
    assert canary not in caplog.text
    assert find_leaks(caplog.text, values) == []


# -- python-hl7's own diagnostics --------------------------------------------


HL7_CANARY = "CANARYHL7Q"
BAD_ESCAPE_ORU = (
    "MSH|^~\\&|RIS|HOSPA|HL7BRIDGE|HUB|20261007120000||ORU^R01|PHL7X|T|2.3\r"
    "PID|1||SA1001^^^HOSPA^MR||ZZZSAMPLE^ONE||19800101|F\r"
    "OBR|1|PL1|ACCQ001|71046^XR^local|R||20261007110000|||||||||||||||20261007120000||CR|F\r"
    f"OBX|1|TX|RPT^Text||{HL7_CANARY}\\XZZ\\ more||||||F\r"
    "OBX|2|ST|S1^Str^99HUB|1|x||||||F\r"
    f"NTE|1||{HL7_CANARY}\\Zlocal\\ note\r"
)


def test_python_hl7_escape_diagnostics_never_reach_the_log(caplog):
    """python-hl7 logs a bad escape with the whole field and a traceback.
    Those records are withheld, on the ingest path and in the mapper."""
    from hl7bridge.v2fhir import message_to_bundle

    conn = connect(":memory:")
    with caplog.at_level(logging.DEBUG):
        IngestPipeline(REGISTRY, conn).handle_bytes(BAD_ESCAPE_ORU.encode())
        with pytest.raises(BridgeError) as info:
            message_to_bundle(BAD_ESCAPE_ORU, REGISTRY)
    conn.close()
    assert str(info.value) == "OBX-5: holds a malformed escape sequence"
    assert "python-hl7 diagnostic withheld" in caplog.text, "nothing logged; test is vacuous"
    assert HL7_CANARY not in caplog.text
    assert "Traceback" not in caplog.text


def test_python_hl7_records_are_withheld_by_the_cli_handler(capsys):
    """The CLI's handler filter withholds them too, whatever the logger."""
    configure_logging(logging.DEBUG)
    record = logging.LogRecord(
        name="some.other.name",
        level=logging.ERROR,
        pathname=__import__("hl7.util").util.__file__,
        lineno=1,
        msg="Error decoding value [%s]",
        args=(HL7_CANARY,),
        exc_info=None,
    )
    logging.getLogger("some.other.name").handle(record)
    err = capsys.readouterr().err
    assert "python-hl7 diagnostic withheld" in err
    assert HL7_CANARY not in err
