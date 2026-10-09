"""Lint precision over the whole generated corpus, as an EXACT set.

This is both the recall test (every seeded message defect is flagged with
the right rule) and the precision test (not one finding anywhere else).
A noisy rule or a missed defect both fail here.
"""

import pytest

from hl7bridge.lint import lint_directory

from .corpus import build_corpus


@pytest.fixture(scope="module")
def linted(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("lint")
    out, registry, manifest = build_corpus(tmp)
    report = lint_directory(out / "messages", registry)
    return report, manifest


def expected_findings(manifest) -> set[tuple[str, str]]:
    """(control id or defect id, rule id) pairs derived from the manifest."""
    category_to_rule = {
        "missing_accession": "HL7-010",
        "bad_timestamp": "HL7-014",
        "unknown_hospital": "HL7-003",
        "unexpected_message_type": "HL7-004",
    }
    expected: set[tuple[str, str]] = set()
    for category, rule in category_to_rule.items():
        for d in manifest.by_category(category):
            expected.add((d.control_id or d.id, rule))
    # parse_error files have no control id; keyed by hospital stream.
    for d in manifest.by_category("parse_error"):
        expected.add((f"garbage:{d.hospital_id}", "HL7-001"))
    return expected


def actual_findings(report) -> set[tuple[str, str]]:
    actual: set[tuple[str, str]] = set()
    for rel_path, findings in report.by_file:
        for f in findings:
            if f.rule_id == "HL7-001":
                hospital_dir = rel_path.split("/")[0]
                actual.add((f"garbage:{hospital_dir}", f.rule_id))
            else:
                actual.add((f.control_id or rel_path, f.rule_id))
    return actual


def test_corpus_findings_are_exactly_the_seeded_defects(linted):
    report, manifest = linted
    assert report.files_checked == 139
    got = actual_findings(report)
    want = expected_findings(manifest)
    assert got - want == set(), f"false positives: {sorted(got - want)}"
    assert want - got == set(), f"missed defects: {sorted(want - got)}"


def test_unknown_hospital_finding_is_unattributed(linted):
    report, _ = linted
    unknown = [f for f in report.findings if f.rule_id == "HL7-003"]
    assert len(unknown) == 1
    assert unknown[0].hospital_id is None


def test_summary_counts_match_findings(linted):
    report, _ = linted
    summary = report.summary()
    assert sum(sum(c.values()) for c in summary.values()) == len(report.findings)
    assert report.error_count == len(report.findings)  # all seeded defects are errors
