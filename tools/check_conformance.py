"""Run the bundled synthetic messages through to-fhir and regenerate the evidence.

Expectations are hand-reviewed IG examples, independent of the mapper/spec code.
Known gaps are evidence, not a failed script; --check detects report drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "conformance"
DOC = ROOT / "docs" / "conformance.md"
MARKER = "<!-- generated conformance evidence -->"


def select(document: dict[str, Any], path: str) -> list[Any]:
    """Select fields, zero-based indexes and list wildcards; missing means []."""
    values: list[Any] = [document]
    for part in path.split("."):
        selected: list[Any] = []
        for value in values:
            if isinstance(value, dict) and part in value:
                selected.append(value[part])
            elif isinstance(value, list):
                if part == "*":
                    selected.extend(value)
                elif part.isdigit() and int(part) < len(value):
                    selected.append(value[int(part)])
        values = selected
    return values


def view(bundle: dict[str, Any]) -> dict[str, Any]:
    """Replace internal fullUrls with resource aliases so reference checks are stable."""
    counts: Counter[str] = Counter()
    encoded = json.dumps(bundle)
    for entry in bundle.get("entry", []):
        kind = entry["resource"]["resourceType"]
        alias = f"{kind}[{counts[kind]}]"
        counts[kind] += 1
        if "fullUrl" in entry:
            encoded = encoded.replace(json.dumps(entry["fullUrl"]), json.dumps(alias))
    normalized = json.loads(encoded)
    document: dict[str, Any] = {"Bundle": normalized}
    for entry in normalized.get("entry", []):
        resource = entry["resource"]
        document.setdefault(resource["resourceType"], []).append(resource)
    return document


def comparable(value: Any) -> str:
    """Compare equivalent timezone spellings without changing the displayed evidence."""
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        value = datetime.fromisoformat(value).astimezone(UTC).isoformat()
    return json.dumps(value, sort_keys=True)


def verdict(
    actual: list[Any], expected: list[Any] | None, exit_code: int, *, unordered: bool = False
) -> str:
    if exit_code:
        return "GAP (rejected)"
    if expected is None:
        return "LOCAL (no IG assertion)"
    left, right = [comparable(v) for v in actual], [comparable(v) for v in expected]
    if unordered:
        left, right = sorted(left), sorted(right)
    return "MATCH" if left == right else "GAP"


def cell(value: str) -> str:
    return value.replace("|", "&#124;").replace("\r", "").replace("\n", "<br>")


def literal(value: Any) -> str:
    return "`" + cell(json.dumps(value, ensure_ascii=True, separators=(",", ":"))) + "`"


def generate(artifacts: Path | None = None) -> str:
    manifest = json.loads((FIXTURES / "expectations.json").read_text(encoding="utf-8"))
    lines = [MARKER, "", "## Results", ""]
    totals: Counter[str] = Counter()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    if artifacts is not None:
        artifacts.mkdir(parents=True, exist_ok=True)
    for case in manifest["cases"]:
        sample = FIXTURES / case["file"]
        relative = sample.relative_to(ROOT).as_posix()
        command = [
            "-m",
            "hl7bridge.cli",
            "to-fhir",
            relative,
            "--config",
            "tests/fixtures/conformance/profiles",
        ]
        run = subprocess.run(
            [sys.executable, *command],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        if run.returncode not in (0, 1):
            raise RuntimeError(f"{case['file']}: CLI exit {run.returncode}: {run.stderr}")
        bundle = json.loads(run.stdout) if run.stdout.strip() else {}
        if not run.returncode and bundle.get("resourceType") != "Bundle":
            raise ValueError(f"{case['file']}: successful CLI did not return a Bundle")
        if run.returncode and (bundle or not run.stderr.strip()):
            raise ValueError(f"{case['file']}: rejection must have no Bundle and a diagnostic")
        if artifacts is not None:
            evidence = {"exit_code": run.returncode, "stderr": run.stderr, "bundle": bundle}
            (artifacts / f"{sample.stem}.json").write_bytes(
                (json.dumps(evidence, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
            )
        document = view(bundle)
        resources = Counter(select(document, "Bundle.entry.*.resource.resourceType"))
        summary = ", ".join(f"{count} {kind}" for kind, count in sorted(resources.items()))
        digest = hashlib.sha256(sample.read_bytes()).hexdigest()
        lines.extend(
            [
                f"### {case['name']}",
                "",
                case["scope"],
                "",
                f"Input: [{case['file']}](../{relative}) (synthetic).",
                f"SHA-256: `{digest}`.",
                "",
                f"CLI exit: **{run.returncode}**. Resources: {summary or 'none'}.",
                "",
            ]
        )
        if run.returncode:
            # Paths use native separators in the CLI; the report is portable.
            diagnostic = run.stderr.strip().replace("\\", "/")
            lines.extend(["```text", diagnostic, "```", ""])
        lines.extend(
            [
                "| Input / output selector | IG expectation | hl7bridge produces | Result |",
                "| --- | --- | --- | --- |",
            ]
        )
        checks = [row for group in case["groups"] for row in manifest["groups"][group]]
        checks.extend(case["checks"])
        for row in checks:
            actual = select(document, row["path"])
            status = verdict(
                actual, row["expected"], run.returncode, unordered=row.get("unordered", False)
            )
            totals[status] += 1
            source = row["source"]
            expectation = row["description"]
            if source:
                expectation += f" ([map]({manifest['sources'][source]}))"
            if row["expected"] is not None:
                expectation += " " + literal(row["expected"])
            produced = literal(actual) if actual else "Absent"
            if run.returncode:
                produced = "No Bundle: CLI rejected the message"
            label = cell(row["input"]) + "<br>" + literal(row["path"])
            lines.append(f"| {label} | {cell(expectation)} | {produced} | **{status}** |")
        lines.append("")
    lines.extend(
        [
            "## Counts",
            "",
            "; ".join(f"{key}: {value}" for key, value in sorted(totals.items())) + ".",
            "",
            "Counts describe only the selected assertions, "
            "including repeated checks across samples.",
            "They are not a conformance percentage or a certification result.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the report has drifted")
    parser.add_argument("--output", type=Path, default=DOC, help="report destination")
    parser.add_argument(
        "--artifacts", type=Path, help="save CLI exit, stderr and raw Bundle per sample"
    )
    args = parser.parse_args()
    # The preamble is maintained prose; all evidence below the marker is generated.
    preamble = DOC.read_text(encoding="utf-8").split(MARKER, 1)[0]
    text = preamble + generate(args.artifacts)
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print("conformance evidence is out of date; run: python tools/check_conformance.py")
            return 1
        print("conformance evidence matches the bundled samples (known gaps included)")
        return 0
    args.output.write_bytes(text.encode("utf-8"))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
