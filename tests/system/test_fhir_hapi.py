"""FHIR system test against a live HAPI server.

Needs HL7B_FHIR_BASE (e.g. http://localhost:8080/fhir). Without it the tests
skip locally, but when HL7B_FHIR_REQUIRED=1 (set by the CI job that runs a
HAPI service container) a missing server FAILS the build instead of skipping:
a check that matters must not silently stop running.

Counts are scoped to this project's identifier systems, so a HAPI instance
with other data in it does not break the assertions.
"""

import os
from datetime import UTC, datetime

import pytest

from hl7bridge.fhir import HapiClient, sync_to_fhir
from hl7bridge.fhir.mapper import accession_system, mrn_system

from .corpus import build_corpus, ingest_corpus

pytestmark = pytest.mark.fhir

BASE = os.environ.get("HL7B_FHIR_BASE")
REQUIRED = os.environ.get("HL7B_FHIR_REQUIRED") == "1"
HOSPITALS = ("HOSPA", "HOSPB", "HOSPC")


@pytest.fixture(scope="module")
def fhir_base() -> str:
    if not BASE:
        if REQUIRED:
            pytest.fail("HL7B_FHIR_REQUIRED=1 but HL7B_FHIR_BASE is not set")
        pytest.skip("HL7B_FHIR_BASE not set; start HAPI FHIR and export it to run these tests")
    return BASE


@pytest.fixture(scope="module")
def synced(tmp_path_factory, fhir_base):
    tmp = tmp_path_factory.mktemp("fhir")
    out, registry, manifest = build_corpus(tmp)
    conn, _pipeline, _acks = ingest_corpus(out, registry, tmp / "fhir.db")
    client = HapiClient(fhir_base)
    if not client.metadata_ok():
        if REQUIRED:
            pytest.fail(f"FHIR server at {fhir_base} did not answer /metadata")
        pytest.skip(f"FHIR server at {fhir_base} did not answer /metadata")
    first = sync_to_fhir(conn, registry, client)
    second = sync_to_fhir(conn, registry, client)
    yield client, registry, manifest, first, second
    conn.close()
    client.close()


def _count_by_identifier_system(client, resource_type, system_fn) -> int:
    return sum(
        client.search_total(resource_type, {"identifier": f"{system_fn(h)}|"}) for h in HOSPITALS
    )


def test_first_sync_posts_expected_counts_with_zero_skips(synced):
    _client, _registry, manifest, first, _second = synced
    expected = manifest.expected["fhir"]
    assert first.skipped == []
    assert first.total("Patient") == expected["patients"]
    assert first.total("ServiceRequest") == expected["service_requests"]
    assert first.total("DiagnosticReport") == expected["diagnostic_reports"]
    assert first.total("ImagingStudy") == expected["imaging_studies"]


def test_server_counts_match_manifest(synced):
    client, _registry, manifest, _first, _second = synced
    expected = manifest.expected["fhir"]
    assert _count_by_identifier_system(client, "Patient", mrn_system) == expected["patients"]
    assert (
        _count_by_identifier_system(client, "ServiceRequest", accession_system)
        == expected["service_requests"]
    )
    assert (
        _count_by_identifier_system(client, "DiagnosticReport", accession_system)
        == expected["diagnostic_reports"]
    )
    # ImagingStudy counted via accession identifier; the orphan study also
    # carries one, so every generated study is covered.
    assert (
        _count_by_identifier_system(client, "ImagingStudy", accession_system)
        == expected["imaging_studies"]
    )


def test_second_sync_is_idempotent(synced):
    client, _registry, manifest, _first, second = synced
    expected = manifest.expected["fhir"]
    assert sum(second.created.values()) == 0, "re-run must not create new resources"
    assert second.skipped == []
    assert _count_by_identifier_system(client, "Patient", mrn_system) == expected["patients"]
    assert (
        _count_by_identifier_system(client, "DiagnosticReport", accession_system)
        == expected["diagnostic_reports"]
    )


def test_corrected_report_is_single_corrected_resource(synced):
    client, _registry, manifest, _first, _second = synced
    for defect in manifest.by_category("corrected_report"):
        system = accession_system(defect.hospital_id)
        bundle = client.read("DiagnosticReport", {"identifier": f"{system}|{defect.accession}"})
        entries = bundle.get("entry", [])
        assert len(entries) == 1, f"{defect.id}: expected exactly one DiagnosticReport"
        resource = entries[0]["resource"]
        assert resource["status"] == "corrected"
        assert "ADDENDUM" in resource.get("conclusion", "")


def test_report_links_to_service_request_by_accession(synced):
    client, _registry, manifest, _first, _second = synced
    defect = manifest.by_category("corrected_report")[0]
    system = accession_system(defect.hospital_id)
    dr = client.read("DiagnosticReport", {"identifier": f"{system}|{defect.accession}"})
    based_on = dr["entry"][0]["resource"]["basedOn"][0]["reference"]
    assert based_on.startswith("ServiceRequest/")
    sr = client.read("ServiceRequest", {"identifier": f"{system}|{defect.accession}"})
    assert based_on.endswith(sr["entry"][0]["resource"]["id"])


def test_run_recorded(synced):
    """Timestamped marker so CI logs show the FHIR job actually exercised a server."""
    client, _registry, _manifest, _first, _second = synced
    assert client.metadata_ok()
    print(f"FHIR system test ran against {client.base_url} at {datetime.now(tz=UTC)}")
