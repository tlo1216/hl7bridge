"""Shared system-test helper: build a populated store from generated data."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hl7bridge.config import HospitalRegistry
from hl7bridge.ingest import IngestPipeline
from hl7bridge.ingest.loaders import load_outbound, load_studies
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.store import connect
from hl7bridge.synth import Manifest, generate

CONFIG_DIR = BUNDLED_PROFILES
# Fixed ingest clock: shortly after the simulated day, before report_now.
INGEST_NOW = datetime(2026, 10, 7, 23, 0, tzinfo=UTC)


def build_corpus(tmp_path: Path, seed: int = 42) -> tuple[Path, HospitalRegistry, Manifest]:
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    out = tmp_path / "synth"
    manifest = generate(out, registry, seed=seed)
    return out, registry, manifest


def ingest_corpus(
    out: Path, registry: HospitalRegistry, db_path: Path
) -> tuple[sqlite3.Connection, IngestPipeline, list[str]]:
    """Feed every generated message through the pipeline (no sockets),
    then load DICOM studies and the outbound log. Returns ACK codes sent."""
    conn = connect(db_path)
    pipeline = IngestPipeline(registry, conn, clock=lambda: INGEST_NOW)
    ack_codes: list[str] = []
    for path in sorted((out / "messages").rglob("*.hl7")):
        ack = pipeline.handle_bytes(path.read_bytes())
        from hl7bridge.ingest.parser import get_field, parse_hl7

        ack_codes.append(get_field(parse_hl7(ack), "MSA-1"))
    load_studies(conn, registry, out / "dicom")
    load_outbound(conn, registry, out / "outbound_reports.jsonl")
    return conn, pipeline, ack_codes
