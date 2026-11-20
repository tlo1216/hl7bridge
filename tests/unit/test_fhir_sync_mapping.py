"""fhir sync with Encounter and the shared code tables, the transaction
check behind ``to-fhir --post``, and the two paths writing the same keys."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import FhirPostError
from hl7bridge.fhir import sync_to_fhir
from hl7bridge.fhir.client import HapiClient
from hl7bridge.ingest import IngestPipeline
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.store import connect
from hl7bridge.v2fhir import bundle_json, message_to_bundle


class FakeServer:
    """Answers conditional PUTs and remembers each resource by its key."""

    def __init__(self) -> None:
        self.by_key: dict[tuple[str, str], dict] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        kind = request.url.path.rsplit("/", 1)[-1]
        key = (kind, request.url.params["identifier"])
        created = key not in self.by_key
        self.by_key[key] = json.loads(request.content)
        return httpx.Response(201 if created else 200, json={"id": str(len(self.by_key))})


@pytest.fixture
def registry() -> HospitalRegistry:
    return HospitalRegistry.load_dir(BUNDLED_PROFILES)


@pytest.fixture
def store(tmp_path, registry):
    conn = connect(tmp_path / "s.db")
    pipeline = IngestPipeline(registry, conn)
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        pipeline.handle_bytes(path.read_bytes())
    yield conn
    conn.close()


def _sync(conn, registry):
    server = FakeServer()
    with HapiClient("http://fhir.test/fhir", transport=httpx.MockTransport(server)) as client:
        report = sync_to_fhir(conn, registry, client)
    return report, server


def test_sync_posts_the_stored_visit_as_an_encounter(store, registry):
    report, server = _sync(store, registry)
    assert report.created["Encounter"] == 1
    assert not report.skipped
    enc = server.by_key[("Encounter", "urn:hl7bridge:hospc:visit|SV0001")]
    # The A08 discharge came after the A01: the stored visit is finished.
    assert enc["status"] == "finished"
    assert enc["class"]["code"] == "IMP"
    assert enc["subject"]["reference"].startswith("Patient/")


def test_sync_skips_a_patient_whose_sex_code_is_not_in_the_table(store, registry, caplog):
    canary = "QZ"
    store.execute("UPDATE patients SET sex = ? WHERE mrn = 'SC1003'", (canary,))
    store.commit()
    with caplog.at_level(logging.DEBUG):
        report, server = _sync(store, registry)
    assert (
        "Patient",
        "HOSPC/SC1003",
        "PID-8: code not in table 0001 (administrative sex); refusing to guess",
    ) in report.skipped
    # Its visit then has no subject, and says so.
    assert ("Encounter", "HOSPC/SV0001", "no resolvable subject patient") in report.skipped
    assert not any(k[0] == "Encounter" for k in server.by_key)
    assert canary not in caplog.text


def test_sync_skips_a_visit_whose_class_is_not_in_the_table(store, registry):
    store.execute("UPDATE encounters SET patient_class = 'Z'")
    store.commit()
    report, _ = _sync(store, registry)
    assert (
        "Encounter",
        "HOSPC/SV0001",
        "PV1-2: code not in table 0004 (patient class); refusing to guess",
    ) in report.skipped


def test_sync_and_to_fhir_write_the_same_keys(store, registry):
    """Both paths key Patient, ServiceRequest, DiagnosticReport and Encounter
    the same way, so posting a message and syncing the store update one
    resource each, never two."""
    _, server = _sync(store, registry)
    synced = {k for k in server.by_key if k[0] != "ImagingStudy"}
    mapped: set[tuple[str, str]] = set()
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        try:
            bundle = bundle_json(message_to_bundle(path.read_bytes().decode(), registry))
        except Exception:  # noqa: BLE001  the refused samples are covered elsewhere
            continue
        for entry in bundle["entry"]:
            ident = entry["resource"]["identifier"][0]
            kind = entry["resource"]["resourceType"]
            if kind != "Observation":  # the store keeps no OBX rows
                mapped.add((kind, f"{ident['system']}|{ident['value']}"))
    # The A40 merge only exists in the store, and the store has no
    # report for the broken samples, so compare where both have the key.
    assert mapped <= synced | {("Patient", "urn:hl7bridge:hospc:mrn|SC1001")}
    assert {k for k in synced if k[0] != "Patient"} <= mapped


def test_sync_and_to_fhir_agree_on_codes(store, registry):
    _, server = _sync(store, registry)
    path = SAMPLES_DIR / "14_hospa_oru_r01_structured.hl7"
    bundle = bundle_json(message_to_bundle(path.read_bytes().decode(), registry))
    mapped = next(
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DiagnosticReport"
    )
    synced = server.by_key[("DiagnosticReport", "urn:hl7bridge:hospa:accession|SACCA0002")]
    for key in ("status", "code", "identifier", "conclusion", "presentedForm"):
        assert synced[key] == mapped[key], key
    assert synced["effectiveDateTime"].startswith("2026-10-07T18:00:00")
    assert synced["issued"].startswith("2026-10-07T19:00:00")


# -- the transaction check -----------------------------------------------------------


def _bundle(registry, name="14_hospa_oru_r01_structured.hl7"):
    return message_to_bundle((SAMPLES_DIR / name).read_bytes().decode(), registry).bundle


def _answer(n: int, status: str = "201 Created", **overrides) -> dict:
    body = {
        "resourceType": "Bundle",
        "type": "transaction-response",
        "entry": [{"response": {"status": status}} for _ in range(n)],
    }
    body.update(overrides)
    return body


def _post(registry, handler):
    with HapiClient("http://fhir.test/fhir", transport=httpx.MockTransport(handler)) as client:
        return client.transaction(_bundle(registry))


def test_a_valid_transaction_response_is_accepted(registry):
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_answer(5))

    got = _post(registry, handler)
    assert seen["method"] == "POST" and seen["path"] == "/fhir"
    assert seen["body"]["type"] == "transaction"
    assert got == [
        ("Patient", 201),
        ("Observation", 201),
        ("Observation", 201),
        ("Observation", 201),
        ("DiagnosticReport", 201),
    ]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_answer(5, type="batch-response"), "not a transaction-response Bundle"),
        ({"resourceType": "OperationOutcome"}, "not a transaction-response Bundle"),
        (_answer(4), "the answer has 4 entries for 5 requests"),
        (_answer(6), "the answer has 6 entries for 5 requests"),
        (_answer(5, entry=None), "the answer has 0 entries for 5 requests"),
        (_answer(5, status="400 Bad Request"), "entry 1 was not committed"),
        (_answer(5, status="2010 Created"), "entry 1 was not committed"),
        (_answer(5, status="Created"), "entry 1 was not committed"),
        (_answer(5, status="204 No Content"), "entry 1 was not committed"),
    ],
)
def test_anything_short_of_a_full_commit_is_an_error(registry, answer, reason):
    with pytest.raises(FhirPostError, match=reason):
        _post(registry, lambda _r: httpx.Response(200, json=answer))


def test_one_failed_entry_among_good_ones_is_an_error(registry):
    answer = _answer(5)
    answer["entry"][3]["response"]["status"] = "412 Precondition Failed"
    with pytest.raises(FhirPostError, match="entry 4 was not committed"):
        _post(registry, lambda _r: httpx.Response(200, json=answer))


def test_a_non_200_answer_is_an_error_without_the_body(registry):
    body = {
        "resourceType": "OperationOutcome",
        "issue": [{"code": "invalid", "diagnostics": "SA1001"}],
    }
    with pytest.raises(FhirPostError) as info:
        _post(registry, lambda _r: httpx.Response(400, json=body))
    assert "SA1001" not in str(info.value)
    assert "invalid" in str(info.value)


def test_an_answer_that_is_not_json_is_an_error(registry):
    with pytest.raises(FhirPostError, match="not JSON"):
        _post(registry, lambda _r: httpx.Response(200, text="<html>"))
