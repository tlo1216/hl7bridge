"""Tamper tests: verify_manifest must FAIL when a seeded defect is removed
or degenerates into something the pipeline would accept. These pin the two
verifier checks that previously passed vacuously."""

from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.synth import generate, verify_manifest

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "hospitals"


@pytest.fixture
def corpus(tmp_path):
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    out = tmp_path / "synth"
    generate(out, registry, seed=42)
    assert verify_manifest(out, registry) == []  # clean baseline
    return out, registry


def _single_file(out: Path, pattern: str) -> Path:
    matches = sorted((out / "messages").rglob(pattern))
    assert len(matches) == 1, f"expected exactly one {pattern}, got {matches}"
    return matches[0]


def test_missing_accession_seed_retyped_is_caught(corpus):
    """If the seeded accession-less ORM stops being an ORM, the defect no
    longer exists and the verifier must say so."""
    out, registry = corpus
    path = _single_file(out, "*_ORMXA0001.hl7")
    path.write_text(path.read_text().replace("ORM^O01", "ADT^A04"), encoding="utf-8")
    problems = verify_manifest(out, registry)
    assert any("HOSPA-missing_accession" in p for p in problems), problems


def test_missing_accession_seed_refacilitied_is_caught(corpus):
    """If the seeded ORM points at an unknown facility, it would quarantine as
    unknown_hospital instead of missing_accession; verifier must flag it."""
    out, registry = corpus
    path = _single_file(out, "*_ORMXA0001.hl7")
    path.write_text(path.read_text().replace("|HOSPA|", "|NOWHERE|"), encoding="utf-8")
    problems = verify_manifest(out, registry)
    assert any("HOSPA-missing_accession" in p for p in problems), problems


def test_bad_timestamp_seed_blanked_is_caught(corpus):
    """An empty OBR-22 is ACCEPTED by the pipeline, so blanking the malformed
    value removes the defect; the verifier must fail, not pass."""
    out, registry = corpus
    path = _single_file(out, "*_ORUXA0001.hl7")
    text = path.read_text()
    assert "2026131599" in text
    path.write_text(text.replace("2026131599", ""), encoding="utf-8")
    problems = verify_manifest(out, registry)
    assert any("HOSPA-bad_timestamp" in p and "empty or absent" in p for p in problems), problems


def test_removed_corrected_report_is_caught(corpus):
    out, registry = corpus
    path = _single_file(out, "*_ORUCA0002.hl7")
    path.unlink()
    problems = verify_manifest(out, registry)
    assert any("HOSPA-corrected_report" in p for p in problems), problems
