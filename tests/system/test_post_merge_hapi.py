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
    return _fresh_registry(tmp_path)


def _fresh_registry(tmp_path: Path) -> tuple[HospitalRegistry, str]:
    tag = uuid.uuid4().hex[:12]
    folder = tmp_path / "profiles"
    folder.mkdir(parents=True)
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
    names = _one(client, "Patient", f"{root}:hospc:mrn", "SC1003")["name"]
    assert "ZZZOTHER" in [n["family"] for n in names]
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


def test_retiring_skips_a_result_whose_parsed_accession_differs(client, run):
    """A result under this report's system and starting with its accession
    and a slash, but whose accession part is another one, stays as it is."""
    registry, root = run
    structured = _sample("14_hospa_oru_r01_structured.hl7")
    _post(client, registry, structured)
    system = f"{root}:hospa:observation"
    report = _one(client, "DiagnosticReport", f"{root}:hospa:accession", "SACCA0002")
    foreign = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"text": "foreign"},
        "identifier": [{"system": system, "value": "SACCA0002/B/sub/1/sub/2"}],
    }
    (created,) = client.commit(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [{"resource": foreign, "request": {"method": "POST", "url": "Observation"}}],
        }
    )
    report["result"].append({"reference": f"Observation/{created.server_id}"})
    client.commit(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": report,
                    "request": {"method": "PUT", "url": f"DiagnosticReport/{report['id']}"},
                }
            ],
        }
    )
    (done,) = _post(client, registry, _corrected(structured))
    assert [c.key.partition("|")[2] for c in done if c.retired] == ["SACCA0002/FIND1/sub/1"]
    kept = client.read_by_id("Observation", str(created.server_id))
    assert kept is not None and kept["status"] == "final"


def test_sync_keeps_the_patient_a_direct_post_keeps(client, run, tmp_path: Path):
    """No ADT: a full-PID ORU, then a thin-PID ORM, then a thin final that
    replaces the first ORU. Direct posting and a sync of a store holding the
    same messages (under fresh systems) leave the same Patient."""
    registry, root = run
    full = "PID|1||SA9001^^^HOSPA^MR||ZZZSAMPLE^NINE||19800101|F|||1 TEST RD^^TESTVILLE^ZZ^00000"
    thin = "PID|1||SA9001^^^HOSPA^MR||ZZZSAMPLE^NINE"

    def with_pid(name: str, pid: str, control: str) -> str:
        lines = [pid if line.startswith("PID|") else line for line in _sample(name).split("\r")]
        text = "\r".join(lines)
        return text.replace("|SMPA0002|", f"|{control}|").replace("|SMPA0003|", f"|{control}|")

    texts = [
        with_pid("03_hospa_oru_r01_final.hl7", full, "SMPA0901").replace("|CR|F", "|CR|P"),
        with_pid("02_hospa_orm_o01.hl7", thin, "SMPA0902"),
        with_pid("03_hospa_oru_r01_final.hl7", thin, "SMPA0903"),
    ]
    _post(client, registry, *texts)
    want = _one(client, "Patient", f"{root}:hospa:mrn", "SA9001")

    other_registry, other_root = _fresh_registry(tmp_path / "other")
    conn = connect(tmp_path / "order.db")
    pipeline = IngestPipeline(other_registry, conn)
    for text in texts:
        pipeline.handle_bytes(text.encode())
    report = sync_to_fhir(conn, other_registry, client)
    conn.close()
    assert report.skipped == []
    got = _one(client, "Patient", f"{other_root}:hospa:mrn", "SA9001")
    for element in ("name", "gender", "birthDate", "address"):
        assert got.get(element) == want.get(element), element
    assert got["address"][0]["city"] == "TESTVILLE"
    final = _one(client, "DiagnosticReport", f"{other_root}:hospa:accession", "SACCA0001")
    assert final["status"] == "final"


# -- ADT^A40 ---------------------------------------------------------------------------


def _a08(mrn: str, family: str) -> str:
    return (
        "MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261007070000||ADT^A08|"
        f"A8{mrn}|T|2.5\rPID|1||{mrn}^^^HOSPC^MR||{family}^ONE\rPV1|1|O\r"
    )


def _a40(survivor: str, gone: str) -> str:
    return (
        "MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261007083000||ADT^A40|"
        f"M{survivor}{gone}|T|2.5\rEVN|A40|20261007083000\r"
        f"PID|1||{survivor}^^^HOSPC^MR||ZZZSAMPLE^THREE|||F\rMRG|{gone}^^^HOSPC^MR\r"
    )


def _patient(client: HapiClient, root: str, mrn: str) -> dict:
    return _one(client, "Patient", f"{root}:hospc:mrn", mrn)


def test_an_a40_links_the_merged_patient_and_keeps_its_data_and_links(client, run):
    registry, root = run
    _post(
        client,
        registry,
        _a08("SC7002", "OLDNAME"),
        _a08("SC7001", "KEEPER"),
        _a08("SC7003", "RELATIVE"),
    )
    gone = _patient(client, root, "SC7002")
    other = {"other": {"reference": f"Patient/{_patient(client, root, 'SC7003')['id']}"}}
    other["type"] = "seealso"
    gone["link"] = [other]
    client.commit(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {"resource": gone, "request": {"method": "PUT", "url": f"Patient/{gone['id']}"}}
            ],
        }
    )
    _post(client, registry, _a40("SC7001", "SC7002"))
    gone = _patient(client, root, "SC7002")
    survivor = _patient(client, root, "SC7001")
    assert gone["active"] is False
    assert gone["name"][0]["family"] == "OLDNAME"
    assert gone["link"] == [
        other,
        {"other": {"reference": f"Patient/{survivor['id']}"}, "type": "replaced-by"},
    ]
    assert "link" not in survivor
    assert [n["family"] for n in survivor["name"]] == ["KEEPER", "ZZZSAMPLE"]
    version = gone["meta"]["versionId"]
    _post(client, registry, _a40("SC7001", "SC7002"))  # a resend changes nothing in content
    assert len(_patient(client, root, "SC7002")["link"]) == 2
    assert int(_patient(client, root, "SC7002")["meta"]["versionId"]) >= int(version)


def test_an_a40_with_no_merged_patient_on_the_server_writes_a_bare_inactive_one(client, run):
    registry, root = run
    _post(client, registry, _a40("SC7011", "SC7012"))
    gone = _patient(client, root, "SC7012")
    survivor = _patient(client, root, "SC7011")
    assert gone["active"] is False and "name" not in gone
    assert gone["link"][0]["other"]["reference"] == f"Patient/{survivor['id']}"


def test_an_a40_chain_and_a_contradicting_or_reversed_merge(client, run):
    from hl7bridge.fhir.post import MergeRefusedError

    registry, root = run
    _post(client, registry, _a40("SC7022", "SC7021"), _a40("SC7023", "SC7022"))
    a, b, c = (_patient(client, root, m) for m in ("SC7021", "SC7022", "SC7023"))
    assert a["link"][0]["other"]["reference"] == f"Patient/{b['id']}"
    assert b["link"][0]["other"]["reference"] == f"Patient/{c['id']}"
    assert "link" not in c
    _post(client, registry, _a40("SC7022", "SC7021"))  # the first edge again: a resend
    with pytest.raises(MergeRefusedError):
        _post(client, registry, _a40("SC7024", "SC7021"))  # a second survivor for A
    with pytest.raises(MergeRefusedError):
        _post(client, registry, _a40("SC7021", "SC7022"))  # B back into A
    assert _patient(client, root, "SC7021")["meta"]["versionId"] == a["meta"]["versionId"]


def test_an_a40_whose_two_mrns_are_one_server_patient_is_refused(client, run):
    from hl7bridge.fhir.post import MergeRefusedError

    registry, root = run
    system = f"{root}:hospc:mrn"
    (done,) = client.commit(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "identifier": [
                            {"system": system, "value": "SC7031"},
                            {"system": system, "value": "SC7032"},
                        ],
                    },
                    "request": {"method": "POST", "url": "Patient"},
                }
            ],
        }
    )
    with pytest.raises(MergeRefusedError):
        _post(client, registry, _a40("SC7031", "SC7032"))
    only = client.read_by_id("Patient", str(done.server_id))
    assert only is not None and "link" not in only and "active" not in only


def test_the_server_rejects_two_writes_to_one_id_as_the_fake_does(client, run):
    registry, root = run
    _post(client, registry, _a08("SC7041", "ONE"))
    patient = _patient(client, root, "SC7041")
    entry = {
        "resource": patient,
        "request": {"method": "PUT", "url": f"Patient/{patient['id']}"},
    }
    with pytest.raises(FhirPostError, match="returned 400"):
        client.commit({"resourceType": "Bundle", "type": "transaction", "entry": [entry, entry]})


def test_sync_posts_a_stored_a40_chain_the_same_as_a_direct_post(client, run, tmp_path: Path):
    registry, root = run
    texts = [_a40("SC7052", "SC7051"), _a40("SC7053", "SC7052"), _a08("SC7051", "LATE")]
    _post(client, registry, *texts)
    want = {m: _patient(client, root, m) for m in ("SC7051", "SC7052", "SC7053")}
    other_registry, other_root = _fresh_registry(tmp_path / "other")
    conn = connect(tmp_path / "a40.db")
    pipeline = IngestPipeline(other_registry, conn)
    for text in texts:
        pipeline.handle_bytes(text.encode())
    report = sync_to_fhir(conn, other_registry, client)
    again = sync_to_fhir(conn, other_registry, client)
    conn.close()
    assert report.skipped == [] and again.skipped == []
    got = {m: _patient(client, other_root, m) for m in want}
    for mrn in want:
        assert got[mrn].get("active") == want[mrn].get("active"), mrn
        assert got[mrn].get("name") == want[mrn].get("name"), mrn
        assert len(got[mrn].get("link", [])) == len(want[mrn].get("link", [])), mrn
