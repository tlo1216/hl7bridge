"""The direct-to-FHIR listener against a fake server: the ACK table, the
failure boundaries, patient matching, run ownership, identity and locks."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import FhirPostError, StoreError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.match import resolve_patient
from hl7bridge.fhir.runscope import RunScope, check_run_id
from hl7bridge.fromfhir import Renderer, pick_mrn
from hl7bridge.ingest.direct import (
    AE_REASONS,
    AR_REASONS,
    COMMITTED,
    DUPLICATE,
    DeadlineExceeded,
    DirectHandler,
    DirectLog,
    KeyLocks,
    Outcome,
    ack_code_for,
    body_hash,
    check_profiles_for_direct,
    outcome_for_error,
)
from hl7bridge.v2fhir.tables import V2_0203

from ..fakefhir import FakeFhir

BASE = "http://fhir.test/fhir"
MRN_SYSTEM = "urn:test:mrn"
LAB_TAG = ("urn:test:tag", "lab")
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
PROFILE = f"""hospital_id: LAB
display_name: Lab
facility_codes: [LAB]
accession_field: OBR-3
timezone: America/New_York
hl7_version: "2.5.1"
timestamps_include_offset: true
expected_message_types: [ADT^A01, ADT^A04, ADT^A08, ADT^A40, ORM^O01, ORU^R01, ACK]
fhir:
  patient_policy: match_only
  identifier_systems:
    mrn: {MRN_SYSTEM}
    accession: urn:test:acc
    placer: urn:test:placer
    visit: urn:test:visit
    observation: urn:test:obs
  tag: {{system: {LAB_TAG[0]}, code: {LAB_TAG[1]}}}
"""


def _patient(mrn: str = "MRN001", **extra) -> dict:
    return {
        "resourceType": "Patient",
        "identifier": [
            {
                "type": {"coding": [{"system": V2_0203, "code": "MR"}]},
                "system": MRN_SYSTEM,
                "value": mrn,
            }
        ],
        "name": [{"family": "ZZZTEST", "given": ["ALPHA"]}],
        "gender": "female",
        "birthDate": "1980-01-01",
        **extra,
    }


@pytest.fixture
def registry(tmp_path) -> HospitalRegistry:
    folder = tmp_path / "profiles"
    folder.mkdir()
    (folder / "lab.yaml").write_text(PROFILE, encoding="utf-8")
    return HospitalRegistry.load_dir(folder)


@pytest.fixture
def server() -> FakeFhir:
    fake = FakeFhir()
    fake._write("Patient", "p1", _patient())
    return fake


class Lab:
    """A handler wired to a fake server, plus the messages for patient p1."""

    def __init__(
        self, registry, server, tmp_path: Path, run: str = "run-a", transport=None
    ) -> None:
        self.server = server
        self.client = HapiClient(BASE, transport=transport or server.transport())
        self.log = DirectLog(tmp_path / f"direct-{run}.db")
        self.scope = RunScope(run, LAB_TAG)
        self.handler = DirectHandler(registry, self.client, self.scope, self.log, clock=lambda: NOW)
        hospital = registry.by_id["LAB"]
        self.r = Renderer(hospital, NOW)
        patient = server.data["Patient"]["p1"]
        self.mrn = pick_mrn(patient)
        self.patient = patient

    def a04(self) -> str:
        return self.r.adt("A04", self.patient, self.mrn, "V1")

    def orm(self) -> str:
        return self.r.orm(self.patient, self.mrn, "ACC1")

    def oru(self, status: str = "F", issued: str = "") -> str:
        return self.r.oru(self.patient, self.mrn, "ACC1", status=status, issued=issued)

    def send(self, text: str) -> tuple[str, str]:
        """(ACK code, MSA-3) for one message."""
        ack = self.handler.handle_bytes(text.encode())
        msa = next(s for s in ack.split("\r") if s.startswith("MSA|")).split("|")
        return msa[1], (msa[3] if len(msa) > 3 else "")


@pytest.fixture
def lab(registry, server, tmp_path) -> Lab:
    return Lab(registry, server, tmp_path)


def _count(server: FakeFhir) -> dict[str, int]:
    return {k: len(v) for k, v in server.data.items()}


# -- T1: the ACK table ---------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(AR_REASONS))
def test_every_ar_reason_gives_ar(reason):
    assert ack_code_for(Outcome(reason, "MSH-9")).code == "AR"


@pytest.mark.parametrize("reason", sorted(AE_REASONS))
def test_every_ae_reason_gives_ae(reason):
    assert ack_code_for(Outcome(reason)).code == "AE"


@pytest.mark.parametrize("reason", [COMMITTED, DUPLICATE])
def test_aa_needs_a_confirmed_commit(reason):
    assert ack_code_for(Outcome(reason, committed=True)).code == "AA"
    unconfirmed = ack_code_for(Outcome(reason))
    assert (unconfirmed.code, unconfirmed.reason) == ("AE", "commit_not_confirmed")


def test_a_reason_nobody_listed_is_never_aa():
    assert ack_code_for(Outcome("something_new", committed=True)).code == "AE"


def _transport_error(name: str) -> FhirPostError:
    err = FhirPostError(0, BASE, reason="transport error")
    err.transport = name
    return err


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (_transport_error("ConnectError"), "fhir_unreachable"),
        (_transport_error("ConnectTimeout"), "fhir_unreachable"),
        (_transport_error("ReadTimeout"), "fhir_timeout"),
        (_transport_error("ReadError"), "fhir_response_lost"),
        (_transport_error("RemoteProtocolError"), "fhir_response_lost"),
        (FhirPostError(200, BASE, reason="not a transaction-response"), "fhir_unconfirmed"),
        (FhirPostError(412, BASE), "fhir_conflict"),
        (FhirPostError(409, BASE), "fhir_conflict"),
        (FhirPostError(422, BASE), "fhir_rejected"),
        (FhirPostError(503, BASE), "fhir_server_error"),
    ],
)
def test_server_failures_map_to_ae_reasons(error, reason):
    outcome = outcome_for_error(error)
    assert outcome.reason == reason
    assert ack_code_for(outcome).code == "AE"


def test_issue_codes_reach_msa3_and_free_text_does_not():
    body = (
        '{"resourceType":"OperationOutcome","issue":[{"code":"processing","diagnostics":"MRN001"}]}'
    )
    outcome = outcome_for_error(FhirPostError(422, BASE, body))
    assert outcome.field == "issue codes processing"


# -- the happy path and patient read-only --------------------------------------------


def test_a04_orm_oru_commit_in_the_run_and_never_touch_the_patient(lab):
    before = dict(lab.server.data["Patient"]["p1"])
    for text in (lab.a04(), lab.orm(), lab.oru()):
        assert lab.send(text) == ("AA", "")
    assert lab.server.data["Patient"]["p1"] == before
    assert len(lab.server.data["Patient"]) == 1
    report = lab.server.by_key("DiagnosticReport", "urn:test:acc/run/run-a|ACC1")
    assert report["subject"] == {"reference": "Patient/p1"}
    assert {"system": "urn:test:acc/as-sent", "value": "ACC1", "use": "secondary"}.items() <= (
        report["identifier"][1].items()
    )
    tags = {(t["system"], t["code"]) for t in report["meta"]["tag"]}
    assert tags == {LAB_TAG, ("urn:test:tag/lab-run", "run-a")}
    for kind in ("Encounter", "ServiceRequest", "Observation"):
        for resource in lab.server.all(kind):
            assert resource["identifier"][0]["system"].endswith("/run/run-a")
            assert ("urn:test:tag/lab-run", "run-a") in {
                (t["system"], t["code"]) for t in resource["meta"]["tag"]
            }
    assert report["basedOn"][0]["identifier"]["system"] == "urn:test:acc/run/run-a"


@pytest.mark.parametrize("event", ["A08", "A40"])
def test_patient_writing_events_get_ar(lab, event):
    text = lab.r.adt(event, lab.patient, lab.mrn, "V1")
    assert lab.send(text) == ("AR", "patient_read_only: MSH-9")
    assert lab.server.transactions == []


def test_an_adt_without_a_visit_number_writes_nothing(lab):
    text = lab.r.adt("A04", lab.patient, lab.mrn, "")
    assert lab.send(text) == ("AE", "nothing_to_write: PV1-19")


def test_production_processing_id_gets_ar(lab):
    text = lab.oru().replace("|T|2.5.1", "|P|2.5.1")
    assert lab.send(text) == ("AR", "refused_processing_id: MSH-11")
    assert lab.server.transactions == []


def test_ack_messages_are_not_written(lab):
    text = lab.oru().replace("ORU^R01", "ACK")
    assert lab.send(text)[0] == "AR"


def test_a_mapping_error_gets_ae_with_the_position(lab):
    text = lab.oru().replace("|N|||F|", "|ZZ|||F|")
    assert lab.send(text) == ("AE", "mapping_error: OBX-8")


# -- identity --------------------------------------------------------------------------


def test_the_same_bytes_twice_are_one_write(lab):
    text = lab.oru()
    assert lab.send(text) == ("AA", "")
    sent = len(lab.server.transactions)
    assert lab.send(text) == ("AA", "")
    assert len(lab.server.transactions) == sent


def test_a_changed_message_under_the_same_control_id_is_a_conflict(lab):
    first = lab.oru()
    assert lab.send(first) == ("AA", "")
    sent = len(lab.server.transactions)
    for changed in (first.replace("5.4", "5.9"), first.replace("20261120", "20261121", 1)):
        assert lab.send(changed) == ("AE", "control_id_conflict: MSH-10")
    assert len(lab.server.transactions) == sent


def test_identity_includes_the_sending_application(lab):
    first = lab.oru()
    assert lab.send(first) == ("AA", "")
    other_app = first.replace("|FROMFHIR|", "|OTHERAPP|").replace("5.4", "5.9")
    assert lab.send(other_app) == ("AA", "")


def test_body_hash_ignores_only_the_segment_terminator():
    text = "MSH|^~\\&|A|B\rPID|1\r"
    assert body_hash(text) == body_hash(text.replace("\r", "\r\n"))
    assert body_hash(text) != body_hash(text.replace("PID|1", "PID|2"))


# -- patient matching (T2) ---------------------------------------------------------------


def _client_for(answer: dict) -> HapiClient:
    return HapiClient(
        BASE, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answer))
    )


def _bundle(n: int, *, total: int | None = None, paged: bool = False, **patient) -> dict:
    body: dict = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {**_patient(), "id": f"p{i}", **patient}} for i in range(n)],
    }
    if total is not None:
        body["total"] = total
    if paged:
        body["link"] = [{"relation": "next", "url": f"{BASE}?_getpages=x"}]
    return body


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (_bundle(0), "unknown"),
        (_bundle(1), "one"),
        (_bundle(2), "ambiguous"),
        (_bundle(1, paged=True), "ambiguous"),  # one per page: still many
        (_bundle(1, total=3), "ambiguous"),
        (_bundle(1, active=False), "inactive"),
        (
            _bundle(1, link=[{"type": "replaced-by", "other": {"reference": "Patient/9"}}]),
            "replaced",
        ),
        (_bundle(1, link=[{"type": "seealso", "other": {"reference": "Patient/9"}}]), "one"),
    ],
)
def test_resolve_patient(answer, outcome):
    with _client_for(answer) as client:
        assert resolve_patient(client, MRN_SYSTEM, "MRN001").outcome == outcome


def test_resolve_patient_asks_for_the_whole_count():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_bundle(1))

    with HapiClient(BASE, transport=httpx.MockTransport(handler)) as client:
        assert resolve_patient(client, MRN_SYSTEM, "MRN001").patient_id == "p0"
    params = dict(seen[0].url.params)
    assert params["_total"] == "accurate" and params["_count"] == "2"


def test_unknown_and_ambiguous_patients_get_ae(lab):
    unknown = lab.oru().replace("MRN001", "MRN404")
    assert lab.send(unknown) == ("AE", "unknown_patient: PID-3")
    lab.server._write("Patient", "p2", _patient())
    assert lab.send(lab.oru()) == ("AE", "ambiguous_patient: PID-3")
    assert "DiagnosticReport" not in lab.server.data


def test_the_mrn_system_follows_the_assigning_authority(tmp_path, server):
    folder = tmp_path / "p"
    folder.mkdir()
    text = PROFILE.replace(
        f"    mrn: {MRN_SYSTEM}\n",
        "    mrn: urn:test:other\n    mrn_by_authority: {LAB: urn:test:mrn}\n",
    )
    (folder / "lab.yaml").write_text(text, encoding="utf-8")
    lab = Lab(HospitalRegistry.load_dir(folder), server, tmp_path)
    assert lab.send(lab.oru()) == ("AA", "")
    other = lab.oru().replace("^^^LAB^MR", "^^^ELSEWHERE^MR").replace("FF2026", "FG2026")
    assert lab.send(other) == ("AE", "unknown_assigning_authority: PID-3.4")


# -- run ownership -------------------------------------------------------------------------


def test_two_runs_keep_their_own_copies(registry, server, tmp_path):
    a = Lab(registry, server, tmp_path, run="run-a")
    b = Lab(registry, server, tmp_path, run="run-b")
    assert a.send(a.oru()) == ("AA", "")
    report_a = dict(server.by_key("DiagnosticReport", "urn:test:acc/run/run-a|ACC1"))
    assert b.send(b.oru()) == ("AA", "")
    assert b.send(b.oru("C")) == ("AA", "")  # B corrects its own copy only
    assert server.by_key("DiagnosticReport", "urn:test:acc/run/run-a|ACC1") == report_a
    assert server.by_key("DiagnosticReport", "urn:test:acc/run/run-b|ACC1")["status"] == "corrected"


def test_a_key_match_without_the_run_tag_is_outside_run(lab):
    planted = {
        "resourceType": "DiagnosticReport",
        "status": "final",
        "code": {"text": "x"},
        "identifier": [{"system": "urn:test:acc/run/run-a", "value": "ACC1"}],
    }
    lab.server._write("DiagnosticReport", "d9", planted)
    before = _count(lab.server)
    assert lab.send(lab.oru()) == ("AE", "outside_run: DiagnosticReport")
    assert _count(lab.server) == before
    assert lab.server.data["DiagnosticReport"]["d9"]["meta"]["versionId"] == "1"


def test_creates_carry_the_run_tag_in_the_condition(lab):
    lab.send(lab.oru())
    entry = lab.server.transactions[-1]["entry"][0]
    assert entry["request"]["method"] == "POST"
    assert "_tag=urn:test:tag/lab-run|run-a" in entry["request"]["ifNoneExist"]


def test_updates_go_by_id_with_if_match(lab):
    lab.send(lab.oru())
    lab.send(lab.oru("C").replace("FF2026", "FH2026"))
    for entry in lab.server.transactions[-1]["entry"]:
        assert entry["request"]["method"] == "PUT"
        assert "?" not in entry["request"]["url"] and entry["request"]["ifMatch"]


def test_run_ids_must_be_short_safe_tokens():
    assert check_run_id("lab-2026-a") == "lab-2026-a"
    for bad in ("", "-x", "a b", "a|b", "x" * 33, "a/b", "a,b"):
        with pytest.raises(Exception, match="value not shown"):
            check_run_id(bad)


# -- stale results ------------------------------------------------------------------------


def test_a_late_older_report_gets_stale_result_and_writes_nothing(lab):
    newer = lab.oru("C", issued="20261120110000-0500")
    assert lab.send(newer) == ("AA", "")
    older = lab.oru("P", issued="20261120100000-0500").replace("FF2026", "FJ2026")
    sent = len(lab.server.transactions)
    assert lab.send(older) == ("AE", "stale_result: OBR-22")
    assert len(lab.server.transactions) == sent
    report = lab.server.by_key("DiagnosticReport", "urn:test:acc/run/run-a|ACC1")
    assert report["status"] == "corrected"


# -- commit first, ACK second: failure boundaries -----------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        {"resourceType": "Bundle", "type": "batch-response", "entry": []},
        {"resourceType": "Bundle", "type": "transaction-response", "entry": []},
        {
            "resourceType": "Bundle",
            "type": "transaction-response",
            # One entry per request (the report and its result), so only the
            # entry status check can refuse it.
            "entry": [{"response": {"status": "400 Bad Request"}}] * 2,
        },
        {"resourceType": "OperationOutcome"},
    ],
)
def test_a_200_without_a_full_transaction_response_is_ae(lab, answer):
    lab.server.before_commit = lambda body: httpx.Response(200, json=answer)
    assert lab.send(lab.oru()) == ("AE", "fhir_unconfirmed")


def test_a_server_4xx_is_ae(lab):
    lab.server.before_commit = lambda body: httpx.Response(
        422, json={"resourceType": "OperationOutcome", "issue": [{"code": "processing"}]}
    )
    assert lab.send(lab.oru()) == ("AE", "fhir_rejected: issue codes processing")


class LoseResponse:
    """Forward to the fake server, then lose the answer (or time out) on the
    first transaction POST: the server committed, the listener never knew."""

    def __init__(self, server: FakeFhir, error: type[Exception]) -> None:
        self.server, self.error, self.fired = server, error, False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        answer = self.server(request)
        if request.method == "POST" and not self.fired:
            self.fired = True
            raise self.error("lost", request=request)
        return answer


@pytest.mark.parametrize(
    ("error", "reason"),
    [(httpx.ReadError, "fhir_response_lost"), (httpx.ReadTimeout, "fhir_timeout")],
)
def test_a_lost_answer_is_ae_and_the_resend_changes_no_counts(
    registry, server, tmp_path, error, reason
):
    lab = Lab(
        registry, server, tmp_path, transport=httpx.MockTransport(LoseResponse(server, error))
    )
    assert lab.send(lab.oru()) == ("AE", reason)
    committed = _count(server)
    assert committed.get("DiagnosticReport") == 1, "the server did commit"
    assert lab.send(lab.oru()) == ("AA", "")
    assert _count(server) == committed


def test_an_unreachable_server_is_ae(registry, server, tmp_path):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    lab = Lab(registry, server, tmp_path, transport=httpx.MockTransport(refuse))
    assert lab.send(lab.oru()) == ("AE", "fhir_unreachable")


def test_a_create_that_another_writer_won_is_ae(lab):
    def interfere(body):
        # Another writer creates the report between our search and commit.
        lab.server._write(
            "DiagnosticReport",
            "x1",
            {
                "resourceType": "DiagnosticReport",
                "identifier": [{"system": "urn:test:acc/run/run-a", "value": "ACC1"}],
                "meta": {"tag": [{"system": "urn:test:tag/lab-run", "code": "run-a"}]},
            },
        )
        lab.server.before_commit = None
        return None

    lab.server.before_commit = interfere
    assert lab.send(lab.oru()) == ("AE", "concurrent_write")
    assert len(lab.server.data["DiagnosticReport"]) == 1


def test_the_same_message_on_two_threads_leaves_one_set(lab):
    text = lab.oru()
    gate = threading.Barrier(2)
    acks: list[tuple[str, str]] = []

    def run():
        gate.wait()
        acks.append(lab.send(text))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(acks) == [("AA", ""), ("AA", "")]
    assert len(lab.server.data["DiagnosticReport"]) == 1
    assert len(lab.server.data["Observation"]) == 1


def test_key_locks_are_taken_in_sorted_order_and_dropped_after():
    locks = KeyLocks()
    with locks.hold(["b", "a", "c", "a"]):
        assert list(locks._locks) == ["a", "b", "c"]
    assert len(locks) == 0, "a lock no one holds is dropped"


def test_a_lock_wait_ends_at_the_deadline():
    locks = KeyLocks()
    with (
        locks.hold(["k"]),
        pytest.raises(DeadlineExceeded),
        locks.hold(["k"], deadline=time.monotonic() + 0.05),
    ):
        pass
    assert len(locks) == 0


# -- the log -------------------------------------------------------------------------------


def test_the_log_refuses_a_file_with_other_tables(tmp_path):
    path = tmp_path / "store.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="not a direct-mode log"):
        DirectLog(path)


def test_rejected_messages_keep_their_bytes_in_the_log(lab):
    text = lab.oru().replace("MRN001", "MRN404")
    lab.send(text)
    row = lab.log.conn.execute("SELECT ack_code, reason, raw FROM direct_log").fetchone()
    assert row[0] == "AE" and row[1] == "unknown_patient" and bytes(row[2]) == text.encode()


def test_profiles_must_all_be_match_only(tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "lab.yaml").write_text(PROFILE.replace("match_only", "create"), encoding="utf-8")
    with pytest.raises(Exception, match="needs match_only"):
        check_profiles_for_direct(HospitalRegistry.load_dir(folder))


# -- the canary ------------------------------------------------------------------------------

CANARY_MRN = "CNRYMRN77"
CANARY_CONTROL = "CNRYCTL55"


def test_no_identifier_reaches_a_log_line_or_msa3(registry, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    server = FakeFhir()
    server._write("Patient", "p1", _patient(CANARY_MRN))
    lab = Lab(registry, server, tmp_path)
    good = lab.oru()
    texts = [good, good, good.replace("5.4", "5.9")]  # AA, duplicate, conflict
    texts.append(lab.oru().replace(CANARY_MRN, CANARY_MRN + "X"))  # unknown patient
    texts.append(lab.oru().replace("|N|||F|", "|ZZ|||F|"))  # mapping error
    texts.append(lab.oru().replace("|T|2.5.1", "|P|2.5.1"))  # refused MSH-11
    acks = []
    for text in texts:
        msh = text.split("\r")[0].split("|")
        msh[9] = CANARY_CONTROL + msh[9][-2:]
        acks.append(lab.handler.handle_bytes(("|".join(msh) + text[text.index("\r") :]).encode()))
    server.before_commit = lambda body: httpx.Response(
        400, json={"resourceType": "OperationOutcome", "issue": [{"code": CANARY_MRN}]}
    )
    acks.append(lab.handler.handle_bytes(lab.oru().replace("FF2026", "FQ2026").encode()))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert caplog.records, "the handler logs each message"
    for value in (CANARY_MRN, CANARY_CONTROL, "ZZZTEST"):
        assert value not in logged
        for ack in acks:
            msa3 = next(s for s in ack.split("\r") if s.startswith("MSA|")).split("|")[3:]
            assert value not in "|".join(msa3)


def test_two_different_messages_under_one_control_id_at_once_give_one_conflict(lab):
    import time

    first = lab.oru()
    second = first.replace("5.4", "5.9")

    def slow(body):
        time.sleep(0.2)  # hold the first commit open while the second arrives
        return None

    lab.server.before_commit = slow
    gate = threading.Barrier(2)
    acks: list[tuple[str, str]] = []

    def go(text: str) -> None:
        gate.wait()
        acks.append(lab.send(text))

    threads = [threading.Thread(target=go, args=(t,)) for t in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(acks) == [("AA", ""), ("AE", "control_id_conflict: MSH-10")]


def test_a_key_that_matches_two_resources_is_ambiguous(lab):
    for n in (1, 2):
        lab.server._write(
            "DiagnosticReport",
            f"d{n}",
            {
                "resourceType": "DiagnosticReport",
                "identifier": [{"system": "urn:test:acc/run/run-a", "value": "ACC1"}],
                "meta": {"tag": [{"system": "urn:test:tag/lab-run", "code": "run-a"}]},
            },
        )
    assert lab.send(lab.oru()) == ("AE", "ambiguous_resource: DiagnosticReport")


def test_two_control_ids_for_one_report_at_once_are_written_one_after_the_other(lab):
    import time

    texts = [lab.oru(), lab.oru()]  # two control ids, the same keys

    def slow(body):
        time.sleep(0.2)  # the first commit is in flight when the second searches
        return None

    lab.server.before_commit = slow
    gate = threading.Barrier(2)
    acks: list[tuple[str, str]] = []

    def go(text: str) -> None:
        gate.wait()
        acks.append(lab.send(text))

    threads = [threading.Thread(target=go, args=(t,)) for t in texts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert acks == [("AA", ""), ("AA", "")]
    assert len(lab.server.data["DiagnosticReport"]) == 1


# -- MLLP TLS review: shutdown and parse-error ACKs ------------------------------------


def test_close_with_a_timeout_returns_while_a_worker_is_busy(lab, caplog):
    gate, started = threading.Event(), threading.Event()

    def stuck() -> None:  # a FHIR call that does not come back
        started.set()
        gate.wait(30)

    lab.handler._executor.submit(stuck)
    assert started.wait(10)  # running, not still queued when close cancels the queue
    start = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING, logger="hl7bridge.ingest.direct"):
            lab.handler.close(timeout=0.3)
        elapsed = time.monotonic() - start
    finally:
        gate.set()
    assert elapsed < 2.0, elapsed
    assert "still busy" in caplog.text


def test_a_parse_error_ack_names_a_readable_control_id(lab):
    text = lab.a04()
    control_id = text.split("\r")[0].split("|")[9]
    ack = lab.handler.handle_bytes(text.encode().replace(b"PID|", b"PID|\xff", 1))
    msa = next(s for s in ack.split("\r") if s.startswith("MSA|")).split("|")
    assert msa[1] == "AR" and msa[2] == control_id


def test_a_commit_in_flight_at_shutdown_still_gets_its_log_row(
    registry, server, tmp_path, monkeypatch
):
    from hl7bridge import cli

    entered, release = threading.Event(), threading.Event()

    def slow(body):
        entered.set()
        release.wait(10)
        return None

    server.before_commit = slow
    logs: list[DirectLog] = []

    class KeptLog(DirectLog):
        def __init__(self, path) -> None:
            super().__init__(path)
            logs.append(self)

    def client(base_url: str, **kwargs) -> HapiClient:
        kwargs.pop("transport", None)
        return HapiClient(base_url, transport=server.transport(), **kwargs)

    patient = server.data["Patient"]["p1"]
    text = Renderer(registry.by_id["LAB"], NOW).adt("A04", patient, pick_mrn(patient), "V1")
    futures = []

    def serve_one(args, handle_frame, handle_junk, where, tls):
        handler = handle_frame.__self__
        futures.append(handler._executor.submit(handler.handle_bytes, text.encode()))
        assert entered.wait(10)  # the worker is inside its FHIR commit
        return 0  # and the listener stops now, as on Ctrl+C

    monkeypatch.setattr(cli, "HapiClient", client)
    monkeypatch.setattr(cli, "DirectLog", KeptLog)
    monkeypatch.setattr(cli, "_serve_forever", serve_one)
    monkeypatch.setattr(cli, "SHUTDOWN_GRACE_S", 0.2)
    args = ["listen", "--target", "fhir", "--fhir-base", BASE, "--run-id", "run-a"]
    args += ["--config", str(tmp_path / "profiles"), "--db", str(tmp_path / "d.db")]
    try:
        rc = cli.main(args)
        release.set()
        ack = futures[0].result(10)
        rows = logs[0].conn.execute("SELECT ack_code, reason FROM direct_log").fetchall()
    finally:
        release.set()
        for kept in logs:
            kept.close()
    assert rc == 0
    assert "\rMSA|AA|" in ack
    assert rows == [("AA", COMMITTED)]
