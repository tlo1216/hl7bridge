"""Engineering rule 3: no PHI in logs.

Runs the full generated corpus through the ingest pipeline and the loaders
with logging captured at DEBUG, then asserts that no synthetic patient name,
MRN, or report text fragment appears in any log record. The synthetic pools
are the ground truth for what counts as 'PHI' here.

The test also asserts that logging DID happen, so it cannot pass vacuously.
"""

import logging
import re
from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.ingest import IngestPipeline
from hl7bridge.ingest.loaders import load_outbound, load_studies
from hl7bridge.store import connect
from hl7bridge.synth import generate
from hl7bridge.synth.generator import (
    BAD_CLOCK_LINE,
    CORRECTED_LINE,
    IMPRESSION,
    N_PATIENTS,
    REPORT_LINES,
)
from hl7bridge.synth.patients import FAMILY_NAMES, GIVEN_NAMES, make_pool

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "hospitals"
MRN_PATTERN = re.compile(r"\bM[ABC]1\d{3}\b")  # matches the synthetic MRN pool
ACCESSION_PATTERN = re.compile(r"\bACCX?[ABC]\d{4}\b")  # synthetic accessions


def _birth_dates() -> set[str]:
    """Every DOB the generator can emit, in raw HL7 and ISO forms."""
    dates: set[str] = set()
    for hosp in ("HOSPA", "HOSPB", "HOSPC"):
        for p in make_pool(hosp, N_PATIENTS):
            dates.add(p.birth_date)
            iso = f"{p.birth_date[0:4]}-{p.birth_date[4:6]}-{p.birth_date[6:8]}"
            dates.add(iso)
    return dates


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("phi")
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    out = tmp / "synth"
    generate(out, registry, seed=42)
    return tmp, out, registry


def test_no_phi_reaches_logs(corpus, caplog):
    tmp, out, registry = corpus
    conn = connect(tmp / "phi.db")
    pipeline = IngestPipeline(registry, conn)
    with caplog.at_level(logging.DEBUG, logger="hl7bridge"):
        for path in sorted((out / "messages").rglob("*.hl7")):
            pipeline.handle_bytes(path.read_bytes())
        load_studies(conn, registry, out / "dicom")
        load_outbound(conn, out / "outbound_reports.jsonl")
    conn.close()

    log_text = caplog.text
    assert "message accepted" in log_text, "pipeline logging is off; test would be vacuous"
    assert "message rejected" in log_text, "quarantine logging is off; test would be vacuous"

    leaks: list[str] = []
    for name in (*FAMILY_NAMES, *GIVEN_NAMES):
        if name in log_text:
            leaks.append(f"patient name {name!r} found in logs")
    mrn_hits = sorted(set(MRN_PATTERN.findall(log_text)))
    leaks.extend(f"MRN {m!r} found in logs" for m in mrn_hits)
    for dob in sorted(_birth_dates()):
        if dob in log_text:
            leaks.append(f"birth date {dob!r} found in logs")
    accession_hits = sorted(set(ACCESSION_PATTERN.findall(log_text)))
    leaks.extend(f"accession {a!r} found in logs" for a in accession_hits)
    for fragment in (*REPORT_LINES, IMPRESSION, CORRECTED_LINE, BAD_CLOCK_LINE):
        if fragment in log_text:
            leaks.append(f"report text {fragment!r} found in logs")
    assert leaks == []
