"""to-fhir transaction Bundles against a live HAPI server.

Needs HL7BRIDGE_FHIR_BASE; CI sets HL7BRIDGE_FHIR_REQUIRED=1 so a missing
server fails instead of skipping (see test_fhir_hapi.py). Posts every
sample that maps, twice: the second round updates in place, so every entry
answers 200 and the resource counts do not change.
"""

from __future__ import annotations

import os

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import MappingError
from hl7bridge.fhir import HapiClient
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.v2fhir import message_to_bundle

pytestmark = pytest.mark.fhir

BASE = os.environ.get("HL7BRIDGE_FHIR_BASE")
REQUIRED = os.environ.get("HL7BRIDGE_FHIR_REQUIRED") == "1"


@pytest.fixture(scope="module")
def posted():
    if not BASE:
        if REQUIRED:
            pytest.fail("HL7BRIDGE_FHIR_REQUIRED=1 but HL7BRIDGE_FHIR_BASE is not set")
        pytest.skip("HL7BRIDGE_FHIR_BASE not set; start HAPI FHIR and export it to run these tests")
    registry = HospitalRegistry.load_dir(BUNDLED_PROFILES)
    bundles = []
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        try:
            bundles.append(message_to_bundle(path.read_bytes().decode(), registry).bundle)
        except MappingError:
            continue
    client = HapiClient(BASE)
    if not client.metadata_ok():
        if REQUIRED:
            pytest.fail(f"FHIR server at {BASE} did not answer /metadata")
        pytest.skip(f"FHIR server at {BASE} did not answer /metadata")
    first = [client.transaction(b) for b in bundles]
    counts = {
        kind: client.search_total(kind, {"identifier": "urn:hl7bridge:hospa:observation|"})
        for kind in ("Observation",)
    }
    second = [client.transaction(b) for b in bundles]
    yield client, first, second, counts
    client.close()


def test_every_entry_is_committed_and_a_resend_updates_in_place(posted):
    client, first, second, counts = posted
    assert all(code in (200, 201) for result in first for _, code in result)
    assert all(code == 200 for result in second for _, code in result)
    after = client.search_total("Observation", {"identifier": "urn:hl7bridge:hospa:observation|"})
    assert after == counts["Observation"]


def test_structured_report_links_its_observations(posted):
    client = posted[0]
    bundle = client.read(
        "DiagnosticReport", {"identifier": "urn:hl7bridge:hospa:accession|SACCA0002"}
    )
    assert bundle.get("total", len(bundle.get("entry", []))) == 1
    report = bundle["entry"][0]["resource"]
    assert len(report["result"]) == 3
    assert report["status"] == "final"


def test_a08_left_the_encounter_finished(posted):
    client = posted[0]
    bundle = client.read("Encounter", {"identifier": "urn:hl7bridge:hospc:visit|SV0001"})
    assert len(bundle["entry"]) == 1
    assert bundle["entry"][0]["resource"]["status"] == "finished"
