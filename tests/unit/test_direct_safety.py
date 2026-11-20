"""Direct mode's safety rules, each with a test that fails when the rule is
taken out: ACK escaping, the log's binding to a server and a run, patient
and authority checks, the deadline and the worker pool, post-commit
failures, reset's guards, from-fhir's output and the mapping hooks."""

from __future__ import annotations

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import UsageError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fromfhir import Renderer, escape, patient_messages, pick_mrn
from hl7bridge.v2fhir import hooks

from ..fakefhir import FakeFhir
from .test_direct import BASE, NOW, PROFILE, Lab, _patient

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
