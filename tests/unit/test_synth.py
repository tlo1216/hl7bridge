"""Generator: deterministic bytes, parseable output, manifest matches artifacts."""

import hashlib
from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import HL7ParseError
from hl7bridge.ingest.parser import parse_hl7
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.synth import generate, load_manifest, verify_manifest

CONFIG_DIR = BUNDLED_PROFILES


@pytest.fixture(scope="module")
def registry():
    return HospitalRegistry.load_dir(CONFIG_DIR)


@pytest.fixture(scope="module")
def generated(tmp_path_factory, registry):
    out = tmp_path_factory.mktemp("synth")
    manifest = generate(out, registry, seed=42)
    return out, manifest


def tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_same_seed_identical_bytes(tmp_path, registry):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a, registry, seed=7)
    generate(b, registry, seed=7)
    assert tree_digest(a) == tree_digest(b)


def test_different_seed_differs(tmp_path, registry, generated):
    other = tmp_path / "other"
    generate(other, registry, seed=43)
    out, _ = generated
    assert tree_digest(other) != tree_digest(out)


def test_every_message_parses_except_seeded_garbage(generated):
    out, manifest = generated
    garbage_count = 0
    for path in sorted((out / "messages").rglob("*.hl7")):
        try:
            parse_hl7(path.read_text(encoding="utf-8"))
        except HL7ParseError:
            garbage_count += 1
    assert garbage_count == len(manifest.by_category("parse_error"))


def test_manifest_round_trips(generated):
    out, manifest = generated
    loaded = load_manifest(out / "manifest.json")
    assert loaded.seed == manifest.seed
    assert {d.id for d in loaded.defects} == {d.id for d in manifest.defects}
    assert loaded.expected == manifest.expected


def test_manifest_matches_artifacts(generated, registry):
    """Every seeded defect must actually be present in the generated output."""
    out, _ = generated
    problems = verify_manifest(out, registry)
    assert problems == []


def test_each_hospital_has_every_defect_family(generated):
    _, manifest = generated
    per_hospital = (
        "order_without_image",
        "image_without_order",
        "mismatch_patient",
        "mismatch_modality",
        "cancelled_order",
        "corrected_report",
        "missing_accession",
        "bad_timestamp",
        "parse_error",
        "ack_errored",
        "ack_rejected",
        "ack_missing",
        "ack_duplicate",
        "ack_unknown_control",
        "unmerged_duplicate",
    )
    for category in per_hospital:
        hospitals = {d.hospital_id for d in manifest.by_category(category)}
        assert {"HOSPA", "HOSPB", "HOSPC"} <= hospitals, category
    assert len(manifest.by_category("unknown_hospital")) == 1
    assert len(manifest.by_category("unexpected_message_type")) == 1


def test_expected_counts_present(generated):
    _, manifest = generated
    assert manifest.expected["quarantine"] == {
        "bad_timestamp": 3,
        "missing_accession": 3,
        "parse_error": 3,
        "unexpected_message_type": 1,
        "unknown_hospital": 1,
    }
    assert manifest.expected["fhir"]["service_requests"] == 30
    assert manifest.expected["fhir"]["diagnostic_reports"] == 24
    assert manifest.expected["fhir"]["imaging_studies"] == 27
    assert manifest.expected["fhir"]["patients"] == 24
    assert manifest.expected["fhir"]["merged_patients"] == 2
    for hosp in ("HOSPA", "HOSPB", "HOSPC"):
        acks = manifest.expected["acks"][hosp]
        assert acks["DELIVERED"] == 5
        assert acks["ERRORED"] == acks["REJECTED"] == acks["NO_ACK"] == 1
        rec = manifest.expected["reconcile"][hosp]
        assert rec["matched"] == 6
        assert len(rec["ORDER_WITHOUT_IMAGE"]) == 1
        assert len(rec["IMAGE_WITHOUT_ORDER"]) == 1


def test_output_has_no_os_newline_translation(generated):
    """HL7 files end segments with a bare CR and the JSON files use LF, on
    every OS. Text-mode writes on Windows would turn LF into CRLF."""
    out, _ = generated
    for path in sorted((out / "messages").rglob("*.hl7")):
        assert b"\n" not in path.read_bytes(), path.name
    for path in (out / "manifest.json", out / "outbound_reports.jsonl"):
        assert b"\r\n" not in path.read_bytes(), path.name
