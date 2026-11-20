"""to-fhir transaction Bundles against a live HAPI server.

Needs HL7BRIDGE_FHIR_BASE; CI sets HL7BRIDGE_FHIR_REQUIRED=1 so a missing
server fails instead of skipping (see test_fhir_hapi.py). Posts every
sample that maps, twice: the second round updates in place, so every entry
answers 200 and the resource counts do not change. The identifier systems
are made fresh for the run, so these resources never mix with the ones
test_fhir_hapi.py counts.
"""

from __future__ import annotations

import os
import uuid

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
def posted(tmp_path_factory):
    if not BASE:
        if REQUIRED:
            pytest.fail("HL7BRIDGE_FHIR_REQUIRED=1 but HL7BRIDGE_FHIR_BASE is not set")
        pytest.skip("HL7BRIDGE_FHIR_BASE not set; start HAPI FHIR and export it to run these tests")
    root = f"urn:hl7bridge-test:{uuid.uuid4().hex[:12]}"
    folder = tmp_path_factory.mktemp("profiles")
    for source in BUNDLED_PROFILES.glob("*.yaml"):
        text = source.read_text(encoding="utf-8") + "fhir:\n  identifier_systems:\n"
        text += "".join(
            f"    {k}: {root}:{source.stem}:{k}\n"
            for k in ("mrn", "accession", "placer", "visit", "observation")
        )
        (folder / source.name).write_text(text, encoding="utf-8")
    registry = HospitalRegistry.load_dir(folder)
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
    observations = {"identifier": f"{root}:hospa:observation|"}
    first = [client.transaction(b) for b in bundles]
    count = client.search_total("Observation", observations)
    second = [client.transaction(b) for b in bundles]
    yield client, root, first, second, count, observations
    client.close()


def test_every_entry_is_committed_and_a_resend_updates_in_place(posted):
    client, _root, first, second, count, observations = posted
    assert all(code in (200, 201) for result in first for _, code in result)
    assert all(code == 200 for result in second for _, code in result)
    assert count == 2
    assert client.search_total("Observation", observations) == count


def test_structured_report_links_its_observations(posted):
    client, root = posted[0], posted[1]
    bundle = client.read("DiagnosticReport", {"identifier": f"{root}:hospa:accession|SACCA0002"})
    assert bundle.get("total", len(bundle.get("entry", []))) == 1
    report = bundle["entry"][0]["resource"]
    assert len(report["result"]) == 2  # the TX line is the conclusion
    assert report["status"] == "final"


def test_a08_left_the_encounter_finished(posted):
    client, root = posted[0], posted[1]
    bundle = client.read("Encounter", {"identifier": f"{root}:hospc:visit|SV0001"})
    assert len(bundle["entry"]) == 1
    assert bundle["entry"][0]["resource"]["status"] == "finished"
