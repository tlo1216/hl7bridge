"""The conformance evidence must come from fresh CLI runs on synthetic fixtures."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from tools.check_conformance import DOC, FIXTURES, ROOT, select, verdict, view


def test_bundled_conformance_evidence(tmp_path):
    report = tmp_path / "conformance.md"
    artifacts = tmp_path / "artifacts"
    run = subprocess.run(
        [
            sys.executable,
            "tools/check_conformance.py",
            "--output",
            str(report),
            "--artifacts",
            str(artifacts),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    generated = report.read_text(encoding="utf-8")
    assert generated == DOC.read_text(encoding="utf-8"), "run: python tools/check_conformance.py"
    evidence = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in artifacts.glob("*.json")}
    assert set(evidence) == {
        "adt_a01",
        "adt_a04",
        "adt_a08",
        "adt_a40",
        "orm_o01",
        "oru_r01",
        "oru_r01_sn",
    }
    for name, result in evidence.items():
        raw = (FIXTURES / f"{name}.hl7").read_bytes()
        assert raw.endswith(b"\r") and b"\n" not in raw
        assert b"ZZZTEST" in raw and b"|T|2.5.1" in raw
        if name == "oru_r01_sn":
            assert result["exit_code"] == 1
            assert result["bundle"] == {}
            assert "OBX-2: code not in table 0125" in result["stderr"]
        else:
            assert result["exit_code"] == 0, result["stderr"]
            assert result["bundle"]["type"] == "transaction"

    merge = view(evidence["adt_a40"]["bundle"])
    assert select(merge, "Patient.1.active") == [False]
    assert select(merge, "Patient.1.identifier.0.value") == ["OLD9001"]
    assert select(merge, "Patient.1.link.0.type") == ["replaced-by"]
    assert select(merge, "Patient.1.link.0.other.reference") == ["Patient[0]"]
    assert select(merge, "Bundle.entry.1.request.method") == ["POST"]
    assert select(merge, "Bundle.entry.1.request.ifNoneExist")

    results = view(evidence["oru_r01"]["bundle"])
    assert select(results, "DiagnosticReport.0.result.*.reference") == [
        "Observation[0]",
        "Observation[1]",
    ]
    assert select(results, "Observation.*.subject.reference") == ["Patient[0]", "Patient[0]"]
    assert select(results, "DiagnosticReport.0.conclusion") == ["SYNTHETIC REPORT ONLY"]
    assert select(results, "Observation.0.valueQuantity.value") == [3.9]
    # A changed conversion must change the comparison, not merely reprint a static pass label.
    results["Observation"][0]["valueQuantity"]["value"] = 99
    assert verdict(select(results, "Observation.0.valueQuantity.value"), [3.9], 0) == "GAP"


def test_check_detects_stale_report_without_rewriting(tmp_path):
    report = tmp_path / "stale.md"
    report.write_text("stale evidence\n", encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "tools/check_conformance.py", "--check", "--output", str(report)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert "out of date" in run.stdout
    assert report.read_text(encoding="utf-8") == "stale evidence\n"


@pytest.mark.parametrize(
    ("actual", "expected", "exit_code", "unordered", "status"),
    [
        (["2026-11-21T10:00:00Z"], ["2026-11-21T11:00:00+01:00"], 0, False, "MATCH"),
        (["2026-11-21T10:00:00Z"], ["2026-11-21T10:00:01Z"], 0, False, "GAP"),
        (["B", "A"], ["A", "B"], 0, True, "MATCH"),
        (["A"], ["A", "A"], 0, True, "GAP"),
        ([], ["Patient"], 0, False, "GAP"),
        ([], None, 0, False, "LOCAL (no IG assertion)"),
        ([], [0.1], 1, False, "GAP (rejected)"),
    ],
)
def test_comparison_preserves_gaps(actual, expected, exit_code, unordered, status):
    assert verdict(actual, expected, exit_code, unordered=unordered) == status
