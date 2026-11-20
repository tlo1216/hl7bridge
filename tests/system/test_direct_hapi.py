"""Direct mode against a live HAPI: the failure-boundary rows marked HAPI.

Needs HL7BRIDGE_FHIR_BASE; HL7BRIDGE_FHIR_REQUIRED=1 makes a missing
server fail instead of skip. Each test makes its own identifier systems,
tag system and Patient, so it never meets another test's data.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.fhir import HapiClient
from hl7bridge.fhir.reset import execute_reset, plan_reset
from hl7bridge.fhir.runscope import RunScope
from hl7bridge.fromfhir import Renderer, pick_mrn
from hl7bridge.ingest.direct import DirectHandler, DirectLog
from hl7bridge.ingest.mllp import send_messages, serve
from hl7bridge.v2fhir.tables import V2_0203

pytestmark = pytest.mark.fhir

BASE = os.environ.get("HL7BRIDGE_FHIR_BASE")
REQUIRED = os.environ.get("HL7BRIDGE_FHIR_REQUIRED") == "1"
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
KINDS = ("Encounter", "ServiceRequest", "Observation", "DiagnosticReport")


@pytest.fixture
def base() -> str:
    if not BASE:
        if REQUIRED:
            pytest.fail("HL7BRIDGE_FHIR_REQUIRED=1 but HL7BRIDGE_FHIR_BASE is not set")
        pytest.skip("HL7BRIDGE_FHIR_BASE not set")
    with HapiClient(BASE) as c:
        if not c.metadata_ok():
            if REQUIRED:
                pytest.fail(f"FHIR server at {BASE} did not answer /metadata")
            pytest.skip(f"FHIR server at {BASE} did not answer /metadata")
    return BASE


class World:
    """Fresh systems, a fresh lab tag and one Patient on the server."""

    def __init__(self, base: str, tmp_path) -> None:
        self.base = base
        self.tmp = tmp_path
        self.tag = uuid.uuid4().hex[:12]
        self.root = f"urn:hl7bridge-direct-test:{self.tag}"
        self.lab_tag = (f"{self.root}:tag", "lab")
        folder = tmp_path / "profiles"
        folder.mkdir()
        systems = "".join(
            f"    {k}: {self.root}:{k}\n"
            for k in ("mrn", "accession", "placer", "visit", "observation")
        )
        (folder / "lab.yaml").write_text(
            "hospital_id: LAB\ndisplay_name: Lab\nfacility_codes: [LAB]\n"
            "accession_field: OBR-3\ntimezone: America/New_York\nhl7_version: '2.5.1'\n"
            "timestamps_include_offset: true\n"
            "expected_message_types: [ADT^A01, ADT^A04, ADT^A08, ORM^O01, ORU^R01]\n"
            "fhir:\n  patient_policy: match_only\n  identifier_systems:\n"
            + systems
            + f"  tag: {{system: {self.lab_tag[0]}, code: lab}}\n",
            encoding="utf-8",
        )
        self.registry = HospitalRegistry.load_dir(folder)
        self.patient = {
            "resourceType": "Patient",
            "identifier": [
                {
                    "type": {"coding": [{"system": V2_0203, "code": "MR"}]},
                    "system": f"{self.root}:mrn",
                    "value": "MRN001",
                }
            ],
            "name": [{"family": "ZZZTEST", "given": ["ALPHA"]}],
            "gender": "female",
        }
        with HapiClient(base) as c:
            (made,) = c.commit(
                {
                    "resourceType": "Bundle",
                    "type": "transaction",
                    "entry": [
                        {"resource": self.patient, "request": {"method": "POST", "url": "Patient"}}
                    ],
                }
            )
        self.patient_id = str(made.server_id)
        self.renderer = Renderer(self.registry.by_id["LAB"], NOW)
        self.mrn = pick_mrn(self.patient)

    def handler(self, run: str, transport: httpx.BaseTransport | None = None) -> DirectHandler:
        client = HapiClient(self.base, transport=transport, timeout=10.0)
        log = DirectLog(self.tmp / f"{run}-{uuid.uuid4().hex[:6]}.db")
        return DirectHandler(self.registry, client, RunScope(run, self.lab_tag), log)

    def oru(self, status: str = "F", issued: str = "") -> str:
        return self.renderer.oru(self.patient, self.mrn, "ACC1", status=status, issued=issued)

    def counts(self, run: str) -> dict[str, int]:
        with HapiClient(self.base) as c:
            return {
                k: c.search_total(k, {"_tag": f"{self.lab_tag[0]}/lab-run|{run}"}) for k in KINDS
            }

    def patient_version(self) -> str:
        with HapiClient(self.base) as c:
            found = c.read_by_id("Patient", self.patient_id)
        assert found is not None
        return str(found["meta"]["versionId"])


def _ack(handler: DirectHandler, text: str) -> tuple[str, str]:
    ack = handler.handle_bytes(text.encode())
    msa = next(s for s in ack.split("\r") if s.startswith("MSA|")).split("|")
    return msa[1], (msa[3] if len(msa) > 3 else "")


@pytest.fixture
def world(base, tmp_path) -> World:
    return World(base, tmp_path)


def test_a04_orm_oru_commit_and_the_patient_stays_read_only(world):
    h = world.handler("run-a")
    r = world.renderer
    texts = [
        r.adt("A04", world.patient, world.mrn, "V1"),
        r.orm(world.patient, world.mrn, "ACC1"),
        world.oru(),
    ]
    for text in texts:
        assert _ack(h, text) == ("AA", "")
    assert world.counts("run-a") == dict.fromkeys(KINDS, 1)
    assert _ack(h, r.adt("A08", world.patient, world.mrn, "V1"))[0] == "AR"
    assert world.patient_version() == "1"


class LoseAnswer(httpx.HTTPTransport):
    """Send the first transaction to HAPI, let it commit, then lose the answer."""

    def __init__(self, error: type[Exception]) -> None:
        super().__init__()
        self.error, self.fired = error, False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        if request.method == "POST" and not self.fired:
            response.read()
            self.fired = True
            raise self.error("lost", request=request)
        return response


@pytest.mark.parametrize(
    ("error", "reason"),
    [(httpx.ReadError, "fhir_response_lost"), (httpx.ReadTimeout, "fhir_timeout")],
)
def test_a_lost_answer_is_ae_and_the_resend_updates_in_place(world, error, reason):
    h = world.handler("run-a", transport=LoseAnswer(error))
    text = world.oru()
    assert _ack(h, text) == ("AE", reason)
    committed = world.counts("run-a")
    assert committed["DiagnosticReport"] == 1, "HAPI committed before the answer was lost"
    assert _ack(h, text) == ("AA", "")
    assert world.counts("run-a") == committed


def test_the_same_message_on_two_connections_leaves_one_set(world):
    h = world.handler("run-a")
    text = world.oru().encode()

    async def run() -> list[bytes]:
        server = await serve("127.0.0.1", 0, h.handle_frame, h.handle_junk)
        port = server.sockets[0].getsockname()[1]
        async with server:
            results = await asyncio.gather(
                send_messages("127.0.0.1", port, [text]), send_messages("127.0.0.1", port, [text])
            )
        return [acks[0] for acks in results]

    acks = asyncio.run(run())
    assert all(b"MSA|AA|" in ack for ack in acks)
    assert world.counts("run-a")["Observation"] == 1
    assert world.counts("run-a")["DiagnosticReport"] == 1


def test_two_threads_racing_on_one_handler_leave_one_set(world):
    h = world.handler("run-a")
    texts = [world.oru(), world.oru()]  # two control ids, one report
    gate = threading.Barrier(2)
    acks: list[tuple[str, str]] = []

    def go(text: str) -> None:
        gate.wait()
        acks.append(_ack(h, text))

    threads = [threading.Thread(target=go, args=(t,)) for t in texts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [a[0] for a in acks] == ["AA", "AA"]
    assert world.counts("run-a")["DiagnosticReport"] == 1


def test_a_late_older_report_is_stale(world):
    h = world.handler("run-a")
    assert _ack(h, world.oru("C", issued="20261120110000-0500")) == ("AA", "")
    assert _ack(h, world.oru("P", issued="20261120100000-0500")) == ("AE", "stale_result: OBR-22")
    with HapiClient(world.base) as c:
        (report,) = c.search_identifier(
            "DiagnosticReport", f"{world.root}:accession/run/run-a", "ACC1"
        )
    assert report["status"] == "corrected"


def test_two_patients_with_one_mrn_are_ambiguous(world):
    # A twin: a second Patient with the same MRN under the same system.
    with HapiClient(world.base) as c:
        c.commit(
            {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {"resource": world.patient, "request": {"method": "POST", "url": "Patient"}}
                ],
            }
        )
    h = world.handler("run-a")
    assert _ack(h, world.oru()) == ("AE", "ambiguous_patient: PID-3")


def test_runs_own_their_resources(world):
    a, b = world.handler("run-a"), world.handler("run-b")
    assert _ack(a, world.oru()) == ("AA", "")
    assert _ack(b, world.oru()) == ("AA", "")
    assert _ack(b, world.oru("C")) == ("AA", "")
    with HapiClient(world.base) as c:
        (ra,) = c.search_identifier("DiagnosticReport", f"{world.root}:accession/run/run-a", "ACC1")
        (rb,) = c.search_identifier("DiagnosticReport", f"{world.root}:accession/run/run-b", "ACC1")
    assert (ra["status"], ra["meta"]["versionId"]) == ("final", "1")
    assert rb["status"] == "corrected"


def test_a_match_without_the_run_tag_is_outside_run(world):
    planted = {
        "resourceType": "DiagnosticReport",
        "status": "final",
        "code": {"text": "planted"},
        "identifier": [{"system": f"{world.root}:accession/run/run-a", "value": "ACC1"}],
    }
    with HapiClient(world.base) as c:
        (made,) = c.commit(
            {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {"resource": planted, "request": {"method": "POST", "url": "DiagnosticReport"}}
                ],
            }
        )
    h = world.handler("run-a")
    assert _ack(h, world.oru()) == ("AE", "outside_run: DiagnosticReport")
    with HapiClient(world.base) as c:
        after = c.read_by_id("DiagnosticReport", str(made.server_id))
    assert after is not None and after["meta"]["versionId"] == "1"
    assert world.counts("run-a")["Observation"] == 0


def test_reset_removes_one_run_only(world):
    r = world.renderer
    for run in ("run-a", "run-b"):
        h = world.handler(run)
        for text in (
            r.adt("A04", world.patient, world.mrn, "V1"),
            r.orm(world.patient, world.mrn, "ACC1"),
            world.oru(),
        ):
            assert _ack(h, text) == ("AA", "")
    with HapiClient(world.base) as c:
        plan = plan_reset(c, RunScope("run-a", world.lab_tag))
        assert plan.total == 4
        assert execute_reset(c, plan) == 4
    assert world.counts("run-a") == dict.fromkeys(KINDS, 0)
    assert world.counts("run-b") == dict.fromkeys(KINDS, 1)
    assert world.patient_version() == "1"


def test_a_refused_delete_in_reset_deletes_nothing(world, monkeypatch):
    """HAPI here does not enforce references on delete, so one refused entry
    is planted: a conditional delete that matches two resources, which HAPI
    refuses. The whole transaction must fail and the run stay whole."""
    from hl7bridge.fhir import reset

    for run in ("run-a", "run-b"):
        assert _ack(world.handler(run), world.oru()) == ("AA", "")
    original = reset.delete_bundle

    def with_a_refused_entry(plan):
        body = original(plan)
        url = f"Observation?_tag={world.lab_tag[0]}|lab"
        body["entry"].append({"request": {"method": "DELETE", "url": url}})
        return body

    monkeypatch.setattr(reset, "delete_bundle", with_a_refused_entry)
    with HapiClient(world.base) as c:
        plan = plan_reset(c, RunScope("run-a", world.lab_tag))
        assert plan.total == 2
        with pytest.raises(Exception):  # noqa: B017  HAPI refuses the planted entry
            execute_reset(c, plan)
    assert world.counts("run-a")["Observation"] == 1
    assert world.counts("run-a")["DiagnosticReport"] == 1
    assert world.counts("run-b")["Observation"] == 1


def test_reset_keeps_a_resource_that_left_the_run_after_the_plan(world):
    assert _ack(world.handler("run-a"), world.oru()) == ("AA", "")
    with HapiClient(world.base) as c:
        plan = plan_reset(c, RunScope("run-a", world.lab_tag))
    obs_id = plan.ids["Observation"][0]
    # HAPI keeps tags across a PUT, so the run tag goes by $meta-delete.
    tag = {"system": f"{world.lab_tag[0]}/lab-run", "code": "run-a"}
    params = {
        "resourceType": "Parameters",
        "parameter": [{"name": "meta", "valueMeta": {"tag": [tag]}}],
    }
    gone = httpx.post(f"{world.base}/Observation/{obs_id}/$meta-delete", json=params)
    assert gone.status_code == 200
    with HapiClient(world.base) as c:
        plan.ids["DiagnosticReport"] = []  # the report still refers to the observation
        execute_reset(c, plan)
        assert c.read_by_id("Observation", obs_id) is not None


def test_a_resend_after_reset_is_written_again(world):
    h = world.handler("run-a")
    text = world.oru()
    assert _ack(h, text) == ("AA", "")
    with HapiClient(world.base) as c:
        execute_reset(c, plan_reset(c, RunScope("run-a", world.lab_tag)))
    assert world.counts("run-a")["DiagnosticReport"] == 0
    assert _ack(h, text) == ("AA", "")
    assert world.counts("run-a")["DiagnosticReport"] == 1
