"""Direct mode's safety rules, each with a test that fails when the rule is
taken out: ACK escaping, the log's binding to a server and a run, patient
and authority checks, the deadline and the worker pool, post-commit
failures, reset's guards, from-fhir's output and the mapping hooks."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import httpx
import pytest

from hl7bridge import cli
from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import ConfigError, FhirPostError, MappingError, StoreError, UsageError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.post import check_not_stale, post_mapped
from hl7bridge.fhir.reset import ResetRefused, execute_reset, plan_reset
from hl7bridge.fhir.runscope import RunScope
from hl7bridge.fromfhir import Renderer, escape, patient_messages, pick_mrn
from hl7bridge.ingest.ack import AckDecision, build_ack
from hl7bridge.ingest.direct import DirectLog, Identity
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.v2fhir import hooks, message_to_bundle

from ..fakefhir import FakeFhir
from .test_direct import BASE, LAB_TAG, NOW, PROFILE, Lab, _count, _patient

RUN_TAG = {"system": "urn:test:tag/lab-run", "code": "run-a"}


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


@pytest.fixture
def lab(registry, server, tmp_path) -> Lab:
    return Lab(registry, server, tmp_path)


@pytest.fixture(autouse=True)
def _no_hooks():
    hooks.reset()
    yield
    hooks.reset()


def _segments(ack: str) -> list[list[str]]:
    return [s.split("|") for s in ack.split("\r") if s]


# -- ACK escaping --------------------------------------------------------------------------


def test_build_ack_escapes_every_echoed_value():
    ack = build_ack(
        decision=AckDecision("AR", "parse_error", ack_text="MSH-3"),
        original_control_id="C|1\rMSA|AA|X",
        ack_control_id="B1",
        sending_app="HUB",
        sending_facility="HUB",
        receiving_app="APP\rMSA|AA|X",
        receiving_facility="F^A~C&D\\E\x1c",
        trigger_event="R01",
        version="2.5\x0b",
        now=NOW,
    )
    segments = _segments(ack)
    assert [s[0] for s in segments] == ["MSH", "MSA"]
    msh, msa = segments
    assert len(msh) == 12 and msh[4] == "APPMSA\\F\\AA\\F\\X"
    assert msh[5] == "F\\S\\A\\R\\C\\T\\D\\E\\E"
    assert msh[11] == "2.5"
    assert msa[:3] == ["MSA", "AR", "C\\F\\1MSA\\F\\AA\\F\\X"] and len(msa) == 4


def test_an_escaped_cr_in_msh3_cannot_forge_an_aa(lab):
    text = lab.oru().replace("|FROMFHIR|", "|APP\\X0D\\MSA\\F\\AA\\F\\X|", 1)
    text = text.replace("|T|2.5.1", "|P|2.5.1", 1)
    ack = lab.handler.handle_bytes(text.encode())
    segments = _segments(ack)
    assert [s[0] for s in segments] == ["MSH", "MSA"]
    assert segments[1][1] == "AR"


# -- the log: bound to the server and the run ----------------------------------------------


def test_a_duplicate_after_the_server_lost_the_data_is_written_again(lab):
    text = lab.oru()
    assert lab.send(text) == ("AA", "")
    for kind in ("DiagnosticReport", "Observation"):
        lab.server.data[kind].clear()  # a reset, or a wiped server
    assert lab.send(text) == ("AA", "")
    assert _count(lab.server)["DiagnosticReport"] == 1
    assert _count(lab.server)["Observation"] == 1


def test_log_rows_vouch_only_for_their_own_server(lab):
    assert lab.send(lab.oru()) == ("AA", "")
    identity = Identity("LAB", "FROMFHIR", "LAB", "FF2026112010000001")
    assert lab.log.prior("run-a", BASE, identity) is not None
    assert lab.log.prior("run-a", "http://other.test/fhir", identity) is None


def test_a_voided_run_takes_a_changed_message_under_an_old_control_id(lab):
    first = lab.oru()
    assert lab.send(first) == ("AA", "")
    changed = first.replace("Synthetic result", "Changed result")
    assert lab.send(changed) == ("AE", "control_id_conflict: MSH-10")
    assert lab.log.void_run("run-a", BASE) >= 1
    assert lab.send(changed) == ("AA", "")


def test_a_maybe_committed_message_holds_its_control_id(registry, server, tmp_path):
    from .test_direct import LoseResponse

    lab = Lab(
        registry,
        server,
        tmp_path,
        transport=httpx.MockTransport(LoseResponse(server, httpx.ReadError)),
    )
    first = lab.oru()
    assert lab.send(first) == ("AE", "fhir_response_lost")
    changed = first.replace("Synthetic result", "Changed result")
    assert lab.send(changed) == ("AE", "control_id_conflict: MSH-10")
    assert server.by_key("DiagnosticReport", "urn:test:acc/run/run-a|ACC1")["status"] == "final"


def test_aa_rows_keep_no_bytes(lab):
    assert lab.send(lab.oru()) == ("AA", "")
    raw, code = lab.log.conn.execute("SELECT raw, ack_code FROM direct_log").fetchone()
    assert code == "AA" and raw == b""


def test_a_second_process_cannot_open_a_held_log(tmp_path):
    first = DirectLog(tmp_path / "held.db")
    try:
        with pytest.raises(StoreError, match="another process"):
            DirectLog(tmp_path / "held.db")
    finally:
        first.close()
    DirectLog(tmp_path / "held.db").close()


# -- patients and authorities --------------------------------------------------------------


def test_one_key_never_moves_to_another_patient(lab):
    assert lab.send(lab.oru()) == ("AA", "")
    lab.server._write("Patient", "p2", _patient("MRN002"))
    other = lab.server.data["Patient"]["p2"]
    text = Renderer(lab.r.hospital, NOW, salt="x").oru(other, pick_mrn(other), "ACC1")
    before = _count(lab.server)
    code, reason = lab.send(text)
    assert (code, reason.split(":")[0]) == ("AE", "patient_mismatch")
    report = lab.server.by_key("DiagnosticReport", "urn:test:acc/run/run-a|ACC1")
    assert report["subject"]["reference"] == "Patient/p1"
    assert _count(lab.server) == before


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"active": False}, "inactive_patient"),
        (
            {"link": [{"type": "replaced-by", "other": {"reference": "Patient/p9"}}]},
            "replaced_patient",
        ),
    ],
)
def test_inactive_and_replaced_patients_get_ae(lab, extra, reason):
    lab.server._write("Patient", "p1", _patient(**extra))
    assert lab.send(lab.oru()) == ("AE", f"{reason}: PID-3")
    assert "DiagnosticReport" not in lab.server.data


def test_the_batch_post_refuses_a_match_only_profile(registry):
    lab_hospital = registry.by_id["LAB"]
    text = Renderer(lab_hospital, NOW).oru(_patient(), pick_mrn(_patient()), "ACC1")
    mapped = message_to_bundle(text, registry)
    server = FakeFhir()
    with (
        HapiClient(BASE, transport=server.transport()) as client,
        pytest.raises(ConfigError, match="match_only"),
    ):
        post_mapped(client, mapped)
    assert server.requests == []


def test_a_correction_retires_only_results_of_the_run(lab):
    assert lab.send(lab.oru()) == ("AA", "")
    report = lab.server.by_key("DiagnosticReport", "urn:test:acc/run/run-a|ACC1")
    stranger = {
        "resourceType": "Observation",
        "status": "final",
        "identifier": [{"system": "urn:test:obs/run/run-a", "value": "ACC1/NUM9/sub/1"}],
    }
    lab.server._write("Observation", "stranger", stranger)
    report["result"].append({"reference": "Observation/stranger"})
    lab.server._write("DiagnosticReport", report["id"], report)
    obx = ["OBX|1|NM|NUM2^Other^99HUB|1|1.0|mmol/L|||||C"]
    text = lab.r.oru(lab.patient, lab.mrn, "ACC1", status="C", obx=obx)
    assert lab.send(text) == ("AA", "")
    own = lab.server.by_key("Observation", "urn:test:obs/run/run-a|ACC1/NUM1/sub/1")
    assert own["status"] == "entered-in-error"
    assert lab.server.data["Observation"]["stranger"]["status"] == "final"


# -- deadline, workers, failures after the commit -------------------------------------------


def test_the_deadline_stops_a_message_before_the_commit(registry, server, tmp_path):
    lab = Lab(registry, server, tmp_path)
    lab.handler.deadline_seconds = 0.0
    assert lab.send(lab.oru()) == ("AE", "deadline_exceeded")
    assert lab.server.transactions == []


def test_frames_run_on_the_handlers_own_workers(lab):
    names: list[str] = []

    def spy(payload, deadline):
        names.append(threading.current_thread().name)
        return "MSH|^~\\&|A\rMSA|AA|1\r"

    lab.handler.handle_bytes = spy  # type: ignore[method-assign]
    asyncio.run(lab.handler.handle_frame(b"x"))
    lab.handler.close()
    assert names and names[0].startswith("hl7bridge-direct")


def test_an_internal_error_is_ar_and_logged(lab):
    def boom(payload, deadline):
        raise RuntimeError("bug")

    lab.handler.handle_bytes = boom  # type: ignore[method-assign]
    ack = asyncio.run(lab.handler.handle_frame(b"x")).decode()
    assert _segments(ack)[1][:2] == ["MSA", "AR"] and "internal_error" in ack
    reason, raw = lab.log.conn.execute("SELECT reason, raw FROM direct_log").fetchone()
    assert (reason, raw) == ("internal_error", b"x")


def test_a_log_failure_after_the_commit_is_ae_not_ar(lab, monkeypatch):
    def fail(**kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(lab.log, "record", fail)
    assert lab.send(lab.oru()) == ("AE", "log_write_failed: see log")
    assert _count(lab.server)["DiagnosticReport"] == 1


def test_a_malformed_answer_is_ae_fhir_unconfirmed(lab, monkeypatch):
    def broken(body, **kw):
        raise KeyError("entry")

    monkeypatch.setattr(lab.client, "commit", broken)
    assert lab.send(lab.oru()) == ("AE", "fhir_unconfirmed")


# -- stale results: one test per branch ----------------------------------------------------

T1 = "2026-11-20T10:00:00-05:00"
T2 = "2026-11-20T11:00:00-05:00"


@pytest.mark.parametrize(
    ("new", "current", "stale"),
    [
        ((T1, "final"), (T2, "final"), True),  # earlier OBR-22, same status
        ((T2, "preliminary"), (T1, "final"), False),  # later OBR-22 wins over status
        ((T1, "preliminary"), (T1, "final"), True),  # same OBR-22, lower status
        ((None, "preliminary"), (None, "final"), True),  # no OBR-22, lower status
        ((None, "final"), (None, "final"), False),  # no OBR-22, same status: a resend
        ((None, "corrected"), (None, "cancelled"), True),  # a retraction outranks
        ((None, "corrected"), (None, "entered-in-error"), True),
        ((None, "cancelled"), (None, "corrected"), False),  # retracting a correction
        ((T1, "final"), (None, "corrected"), True),  # one side without OBR-22: status
    ],
)
def test_stale_rule_branches(new, current, stale):
    def report(pair):
        issued, status = pair
        return {"status": status, **({"issued": issued} if issued else {})}

    if stale:
        with pytest.raises(Exception, match="newer version"):
            check_not_stale(report(current), report(new))
    else:
        check_not_stale(report(current), report(new))


# -- fhir reset ------------------------------------------------------------------------------


def _tagged(kind: str, server_id: str) -> dict:
    return {"resourceType": kind, "id": server_id, "meta": {"tag": [RUN_TAG]}}


def _search_answer(*resources: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.url.path.rsplit("/", 1)[-1]
        entries = [{"resource": r} for r in resources if kind == "Observation"]
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": entries})

    return httpx.MockTransport(handler)


def test_reset_refuses_an_id_that_is_not_a_fhir_id():
    with (
        HapiClient(BASE, transport=_search_answer(_tagged("Observation", "1/../x"))) as client,
        pytest.raises(ResetRefused, match="not a FHIR id"),
    ):
        plan_reset(client, RunScope("run-a", LAB_TAG))


def test_reset_refuses_a_search_answer_of_another_type():
    with (
        HapiClient(BASE, transport=_search_answer(_tagged("Patient", "p1"))) as client,
        pytest.raises(FhirPostError, match="another resource type"),
    ):
        plan_reset(client, RunScope("run-a", LAB_TAG))


def test_reset_keeps_a_resource_retagged_after_the_plan(lab):
    assert lab.send(lab.oru()) == ("AA", "")
    with HapiClient(BASE, transport=lab.server.transport()) as client:
        plan = plan_reset(client, RunScope("run-a", LAB_TAG))
        obs_id = plan.ids["Observation"][0]
        moved = lab.server.data["Observation"][obs_id]
        moved["meta"]["tag"] = [{"system": "urn:test:tag/lab-run", "code": "run-b"}]
        moved["result_of"] = "none"
        lab.server.data["DiagnosticReport"].clear()
        plan.ids["DiagnosticReport"] = []
        execute_reset(client, plan)
    assert obs_id in lab.server.data["Observation"]


def test_reset_does_not_take_202_for_a_delete(lab):
    assert lab.send(lab.oru()) == ("AA", "")

    def queued(body):
        answers = [{"response": {"status": "202 Accepted"}} for _ in body["entry"]]
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "transaction-response", "entry": answers}
        )

    with HapiClient(BASE, transport=lab.server.transport()) as client:
        plan = plan_reset(client, RunScope("run-a", LAB_TAG))
        lab.server.before_commit = queued
        with pytest.raises(FhirPostError, match="was not committed"):
            execute_reset(client, plan)


def test_a_paged_identifier_search_with_no_entries_is_not_none():
    def handler(request):
        return httpx.Response(
            200,
            json={"resourceType": "Bundle", "link": [{"relation": "next", "url": BASE + "?p=2"}]},
        )

    with (
        HapiClient(BASE, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(FhirPostError, match="no entries"),
    ):
        client.search_identifier("DiagnosticReport", "urn:x", "A1")


# -- from-fhir ------------------------------------------------------------------------------


def test_escape_drops_every_control_character():
    assert escape("A\x0bB\x1cC\tD\rE\nF") == "A B C D E F"


def test_a_birth_date_that_is_not_a_fhir_date_is_refused(registry):
    r = Renderer(registry.by_id["LAB"], NOW)
    patient = _patient(birthDate="1980-01-01\rOBX|9|ST|X||INJECTED")
    with pytest.raises(UsageError, match="birthDate"):
        r.pid(patient, pick_mrn(patient))


def test_from_fhir_takes_only_a_known_mrn_system(registry):
    server = FakeFhir()
    server._write(
        "Patient",
        "p7",
        {"resourceType": "Patient", "identifier": [{"system": "urn:x", "value": "1"}]},
    )
    with (
        HapiClient(BASE, transport=server.transport()) as client,
        pytest.raises(UsageError, match="MRN system"),
    ):
        patient_messages(client, "p7", registry.by_id["LAB"], NOW)


def test_from_fhir_skips_an_mr_identifier_under_another_system(registry):
    server = FakeFhir()
    patient = _patient()
    stranger = {**patient["identifier"][0], "system": "urn:x", "value": "OTHER1"}
    patient["identifier"] = [stranger, {"system": "urn:test:mrn", "value": "MRN001"}]
    server._write("Patient", "p8", patient)
    with HapiClient(BASE, transport=server.transport()) as client:
        files, mrn = patient_messages(client, "p8", registry.by_id["LAB"], NOW)
    assert (mrn.value, mrn.typed_mr) == ("MRN001", False)
    assert "|MRN001^^^^MR|" in files["04_oru_r01.hl7"]


def test_the_profile_mrn_system_gets_an_empty_authority(registry):
    r = Renderer(registry.by_id["LAB"], NOW)
    assert "|MRN001^^^^MR|" in r.pid(_patient(), pick_mrn(_patient()))


def test_two_patients_in_one_second_get_their_own_ids(registry):
    server = FakeFhir()
    server._write("Patient", "p1", _patient())
    server._write("Patient", "p2", _patient("MRN002"))
    with HapiClient(BASE, transport=server.transport()) as client:
        one, _ = patient_messages(client, "p1", registry.by_id["LAB"], NOW)
        two, _ = patient_messages(client, "p2", registry.by_id["LAB"], NOW)
    for name in one:
        msh_one, msh_two = one[name].split("|")[9], two[name].split("|")[9]
        assert msh_one != msh_two and len(msh_one) <= 20
    assert one["03_orm_o01.hl7"].split("ORC|NW|")[1] != two["03_orm_o01.hl7"].split("ORC|NW|")[1]


# -- mapping hooks and listen ---------------------------------------------------------------


def test_listen_refuses_a_mapping_module_off_loopback(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    hook = tmp_path / "hooks.py"
    hook.write_text("def sex_code(raw):\n    return 'male'\n", encoding="utf-8")
    args = ["listen", "--target", "fhir", "--fhir-base", BASE, "--run-id", "a"]
    args += ["--host", "0.0.0.0", "--mapping-module", str(hook)]
    args += ["--config", str(BUNDLED_PROFILES / "examples")]
    assert cli.main(args) == 2
    assert "loopback" in capsys.readouterr().err
    assert hooks.active() == ()


@pytest.mark.parametrize(
    "body",
    [
        "def sex_code(raw):\n    return ['male']\n",  # unhashable answer
        "def sex_code(raw):\n    raise SystemExit(3)\n",
    ],
)
def test_a_misbehaving_hook_is_a_mapping_error(tmp_path, body):
    path = tmp_path / "hooks.py"
    path.write_text(body, encoding="utf-8")
    hooks.load_mapping_module(path)
    with pytest.raises(MappingError, match="sex_code"):
        hooks.sex_code("F", lambda raw: "female")


@pytest.mark.parametrize(
    "body",
    [
        "import functools\n\n\ndef _f(a, raw):\n    return a\n\n\n"
        "sex_code = functools.partial(_f, 'male')\n",
        "from os.path import basename as sex_code\n",
    ],
)
def test_a_hook_name_bound_to_a_non_function_is_refused(tmp_path, body):
    path = tmp_path / "hooks.py"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="sex_code"):
        hooks.load_mapping_module(path)


def test_a_hook_file_can_resolve_its_own_type_hints(tmp_path):
    path = tmp_path / "hooks.py"
    path.write_text(
        "from __future__ import annotations\n\nimport typing\n\n\n"
        "class _Code(str):\n    pass\n\n\nclass _Row:\n    code: _Code\n\n\n"
        "_HINTS = typing.get_type_hints(_Row)\n\n\n"
        "def sex_code(raw):\n    return 'male'\n",
        encoding="utf-8",
    )
    assert hooks.load_mapping_module(path) == ("sex_code",)
