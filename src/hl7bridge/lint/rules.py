"""Lint rule catalog.

Rule discipline: a small, high-precision set. Every rule
has a test proving it fires on a bad message and stays silent on a good
one, and the whole generated corpus is a precision fixture: the only
findings allowed are the seeded defects, asserted as an exact set.

Severity: 'error' means the message would break downstream processing
(quarantine-grade); 'warning' means degraded but processable.
"""

from __future__ import annotations

from dataclasses import dataclass

# rule id -> (severity, summary)
KNOWN_RULES: dict[str, tuple[str, str]] = {
    "HL7-001": ("error", "message is not parseable as HL7 v2"),
    "HL7-002": ("error", "MSH-10 message control id is missing"),
    "HL7-003": ("error", "MSH-4/MSH-6 match no configured hospital"),
    "HL7-004": ("error", "message type not expected for this hospital"),
    "HL7-005": ("error", "MSH-7 message timestamp is unparseable"),
    "HL7-006": ("warning", "MSH-7 message timestamp is missing"),
    "HL7-010": ("error", "accession missing from the hospital's configured field"),
    "HL7-011": ("warning", "ORM without a procedure code in OBR-4"),
    "HL7-012": ("error", "PID-3 patient identifier is missing"),
    "HL7-013": ("error", "ORU OBR-25 result status is not P, F, or C"),
    "HL7-014": ("error", "ORU OBR-22 result timestamp is unparseable"),
    "HL7-015": ("error", "ORU OBR-7 observation timestamp is unparseable"),
    "HL7-016": ("error", "ORM ORC-1 order control is not NW or CA"),
    "HL7-020": ("error", "ACK MSA segment missing or MSA-1 not AA/AE/AR"),
}


@dataclass(frozen=True, slots=True)
class LintFinding:
    rule_id: str
    severity: str
    hospital_id: str | None
    control_id: str | None
    field: str
    message: str


def make_finding(
    rule_id: str,
    *,
    hospital_id: str | None,
    control_id: str | None,
    field: str,
    detail: str = "",
) -> LintFinding:
    severity, summary = KNOWN_RULES[rule_id]
    return LintFinding(
        rule_id=rule_id,
        severity=severity,
        hospital_id=hospital_id,
        control_id=control_id,
        field=field,
        message=f"{summary}{': ' + detail if detail else ''}",
    )
