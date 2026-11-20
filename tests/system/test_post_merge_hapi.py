"""The shared post (merge and retire) and sync agreement, against a live HAPI.

Needs HL7BRIDGE_FHIR_BASE; HL7BRIDGE_FHIR_REQUIRED=1 makes a missing
server fail instead of skip (see test_fhir_hapi.py). Every test writes
under identifier systems made fresh for the run (``urn:hl7bridge-test:
<run>:...``), so it never meets data from another test or an earlier run.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import FhirPostError, MappingError
from hl7bridge.fhir import HapiClient, sync_to_fhir
from hl7bridge.fhir.post import post_mapped
from hl7bridge.ingest import IngestPipeline
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.store import connect
from hl7bridge.v2fhir import message_to_bundle

pytestmark = pytest.mark.fhir

BASE = os.environ.get("HL7BRIDGE_FHIR_BASE")
REQUIRED = os.environ.get("HL7BRIDGE_FHIR_REQUIRED") == "1"
KINDS = ("mrn", "accession", "placer", "visit", "observation")


@pytest.fixture
def client():
    if not BASE:
        if REQUIRED:
            pytest.fail("HL7BRIDGE_FHIR_REQUIRED=1 but HL7BRIDGE_FHIR_BASE is not set")
        pytest.skip("HL7BRIDGE_FHIR_BASE not set; start HAPI FHIR and export it to run these tests")
    with HapiClient(BASE) as c:
        if not c.metadata_ok():
            if REQUIRED:
                pytest.fail(f"FHIR server at {BASE} did not answer /metadata")
            pytest.skip(f"FHIR server at {BASE} did not answer /metadata")
        yield c


@pytest.fixture
def run(tmp_path) -> tuple[HospitalRegistry, str]:
    """The bundled profiles with identifier systems of this run only."""
    tag = uuid.uuid4().hex[:12]
    folder = tmp_path / "profiles"
    folder.mkdir()
    for source in BUNDLED_PROFILES.glob("*.yaml"):
        text = source.read_text(encoding="utf-8")
        text += "fhir:\n  identifier_systems:\n" + "".join(
            f"    {k}: urn:hl7bridge-test:{tag}:{source.stem}:{k}\n" for k in KINDS
        )
        (folder / source.name).write_text(text, encoding="utf-8")
    return HospitalRegistry.load_dir(folder), f"urn:hl7bridge-test:{tag}"


def _sample(name: str) -> str:
    return (SAMPLES_DIR / name).read_bytes().decode()


def _one(client: HapiClient, kind: str, system: str, value: str) -> dict:
    found = client.search_identifier(kind, system, value)
    assert len(found) == 1, (kind, value, len(found))
    return found[0]


def _post(client: HapiClient, registry: HospitalRegistry, *texts: str):
    return [post_mapped(client, message_to_bundle(t, registry)) for t in texts]


def test_a_thin_oru_does_not_erase_the_patient(client, run):
    registry, root = run
    a04 = _sample("01_hospa_adt_a04.hl7").replace(
        "|19800101|F", "|19800101|F|||1 TEST RD^^TESTVILLE^ZZ^00000||555-0101"
    )
    thin = _sample("03_hospa_oru_r01_final.hl7").replace("ZZZSAMPLE^ONE||19800101|F", "")
    _post(client, registry, a04, thin)
    patient = _one(client, "Patient", f"{root}:hospa:mrn", "SA1001")
    assert patient["address"][0]["city"] == "TESTVILLE"
    assert patient["telecom"][0]["value"] == "555-0101"
    assert patient["gender"] == "female"
    assert patient["meta"]["versionId"] == "1", "the ORU must not write the Patient"
    report = _one(client, "DiagnosticReport", f"{root}:hospa:accession", "SACCA0001")
    assert report["subject"]["reference"] == f"Patient/{patient['id']}"


def test_an_a08_without_admit_time_keeps_it(client, run):
    registry, root = run
    a08 = (
        "MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261008100000-0400||ADT^A08|CQ8|T|2.5\r"
        "EVN|A08|20261008100000-0400\r"
        "PID|1||SC1003^^^HOSPC^MR||ZZZSAMPLE^FOUR^Q||19650412|M\r"
        "PV1|1|I|||||||||||||||||SV0001\r"
    )
    _post(client, registry, _sample("13_hospc_adt_a01_inpatient.hl7"), a08)
    enc = _one(client, "Encounter", f"{root}:hospc:visit", "SV0001")
    assert enc["period"]["start"] == "2026-10-07T15:55:00Z"
    _post(client, registry, _sample("15_hospc_adt_a08_discharge.hl7"), a08)
    enc = _one(client, "Encounter", f"{root}:hospc:visit", "SV0001")
    assert enc["status"] == "finished"
    assert enc["period"] == {"start": "2026-10-07T15:55:00Z", "end": "2026-10-09T13:30:00Z"}


def _corrected(text: str) -> str:
    lines = [line for line in text.split("\r") if not line.startswith(("OBX|2|", "NTE|"))]
    return (
        "\r".join(lines)
        .replace("|US|F", "|US|C")
        .replace("20261007120000||US", "20261007130000||US")
        .replace("SMPA0005", "SMPA0105")
    )


def test_a_correction_retires_the_result_it_dropped(client, run):
    registry, root = run
    structured = _sample("14_hospa_oru_r01_structured.hl7")
    _post(client, registry, structured)
    (done,) = _post(client, registry, _corrected(structured))
    assert [c.kind for c in done if c.retired] == ["Observation"]
    system = f"{root}:hospa:observation"
    finding = _one(client, "Observation", system, "SACCA0002/FIND1/sub/1")
    measure = _one(client, "Observation", system, "SACCA0002/MEAS1/sub/1")
    assert finding["status"] == "entered-in-error"
    assert measure["status"] == "final"
    assert measure["valueQuantity"]["value"] == 12.5
    report = _one(client, "DiagnosticReport", f"{root}:hospa:accession", "SACCA0002")
    assert report["status"] == "corrected"
    assert [r["reference"] for r in report["result"]] == [f"Observation/{measure['id']}"]
    (again,) = _post(client, registry, _corrected(structured))
    assert not any(c.retired for c in again)


def test_a_stale_version_fails_the_whole_transaction(client, run):
    """If-Match on the version read: a write in between is never lost."""
    registry, root = run
    _post(client, registry, _sample("13_hospc_adt_a01_inpatient.hl7"))
    mapped = message_to_bundle(_sample("15_hospc_adt_a08_discharge.hl7"), registry)
    from hl7bridge.fhir.post import prepare

    body = prepare(client, mapped)
    # Someone else writes the Patient after our read.
    patient = _one(client, "Patient", f"{root}:hospc:mrn", "SC1003")
    (other,) = _post(
        client,
        registry,
        _sample("13_hospc_adt_a01_inpatient.hl7").replace("ZZZSAMPLE^FOUR", "ZZZOTHER^FOUR"),
    )
    assert other
    # HAPI answers a failed If-Match inside a transaction with 409 Conflict.
    with pytest.raises(FhirPostError, match=r"returned (409|412)"):
        client.commit(body)
    enc = _one(client, "Encounter", f"{root}:hospc:visit", "SV0001")
    assert enc["status"] == "in-progress", "nothing of the failed transaction was written"
    assert _one(client, "Patient", f"{root}:hospc:mrn", "SC1003")["name"][0]["family"] == (
        "ZZZOTHER"
    )
    assert patient


def _snapshot(client: HapiClient, root: str, keys: dict[str, list[tuple[str, str]]]) -> dict:
    found = {}
    ids = {}
    for kind, pairs in keys.items():
        for system, value in pairs:
            resource = _one(client, kind, system, value)
            ids[f"{kind}/{resource['id']}"] = f"{kind} {value}"
            found[(kind, value)] = resource

    def clean(value):
        if isinstance(value, dict):
            return {
                k: (ids.get(v, v) if k == "reference" else clean(v))
                for k, v in value.items()
                if k not in ("id", "meta")
            }
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return {key: clean(r) for key, r in found.items()}


def test_sync_after_to_fhir_post_writes_the_same_content(client, run, tmp_path: Path):
    """Post the samples with the to-fhir path, then sync a store that took
    the same messages to the same server: every resource stays as it was."""
    registry, root = run
    texts = []
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        text = path.read_bytes().decode()
        try:
            message_to_bundle(text, registry)
        except MappingError:
            continue
        texts.append(text)
    _post(client, registry, *texts)
    keys = {
        "Patient": [(f"{root}:hospa:mrn", "SA1001"), (f"{root}:hospc:mrn", "SC1003")],
        "Encounter": [(f"{root}:hospc:visit", "SV0001")],
        "ServiceRequest": [(f"{root}:hospa:accession", "SACCA0001")],
        "Observation": [
            (f"{root}:hospa:observation", "SACCA0002/MEAS1/sub/1"),
            (f"{root}:hospa:observation", "SACCA0002/FIND1/sub/1"),
        ],
        "DiagnosticReport": [
            (f"{root}:hospa:accession", "SACCA0001"),
            (f"{root}:hospa:accession", "SACCA0002"),
        ],
    }
    before = _snapshot(client, root, keys)
    conn = connect(tmp_path / "s.db")
    pipeline = IngestPipeline(registry, conn)
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        pipeline.handle_bytes(path.read_bytes())
    report = sync_to_fhir(conn, registry, client)
    conn.close()
    assert report.skipped == []
    assert sum(report.created.values()) == 0
    after = _snapshot(client, root, keys)
    assert after == before
