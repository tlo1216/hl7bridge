"""Loaders enforce the store's UTC invariant at the admission gate."""

import json
from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import BridgeError
from hl7bridge.ingest.loaders import StudyLoadError, load_outbound, load_studies
from hl7bridge.store import connect

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "hospitals"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "l.db")
    yield c
    c.close()


def write_jsonl(tmp_path: Path, *entries) -> Path:
    p = tmp_path / "outbound.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return p


def entry(**overrides):
    base = {
        "hospital_id": "HOSPA",
        "control_id": "RPT1",
        "accession": "ACC1",
        "sent_at_utc": "2026-10-07T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_valid_entry_loads_and_normalizes_to_utc(conn, tmp_path):
    # A non-UTC offset is accepted but stored normalized to UTC.
    path = write_jsonl(tmp_path, entry(sent_at_utc="2026-10-07T05:00:00-07:00"))
    assert load_outbound(conn, path) == 1
    row = conn.execute("SELECT sent_at_utc FROM outbound_reports").fetchone()
    assert row["sent_at_utc"] == "2026-10-07T12:00:00+00:00"


def test_naive_sent_at_rejected_with_line_number(conn, tmp_path):
    path = write_jsonl(
        tmp_path, entry(), entry(control_id="RPT2", sent_at_utc="2026-10-07T12:00:00")
    )
    with pytest.raises(BridgeError, match=r":2: sent_at_utc .* is naive"):
        load_outbound(conn, path)


def test_garbage_sent_at_rejected(conn, tmp_path):
    path = write_jsonl(tmp_path, entry(sent_at_utc="yesterday"))
    with pytest.raises(BridgeError, match="bad sent_at_utc"):
        load_outbound(conn, path)


def test_non_string_sent_at_rejected(conn, tmp_path):
    path = write_jsonl(tmp_path, entry(sent_at_utc=1759838400))
    with pytest.raises(BridgeError, match="bad sent_at_utc"):
        load_outbound(conn, path)


def test_missing_key_rejected(conn, tmp_path):
    bad = entry()
    del bad["accession"]
    path = write_jsonl(tmp_path, bad)
    with pytest.raises(BridgeError, match=r"missing keys \['accession'\]"):
        load_outbound(conn, path)


def test_empty_file_rejected(conn, tmp_path):
    path = tmp_path / "outbound.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(BridgeError, match="no outbound report entries"):
        load_outbound(conn, path)


def test_load_studies_empty_dir_rejected(conn, tmp_path):
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    with pytest.raises(StudyLoadError, match=r"no .*dcm files"):
        load_studies(conn, registry, tmp_path)
