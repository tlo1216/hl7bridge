"""Lint execution: one message or a whole directory of .hl7 files."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..config import HospitalConfig, HospitalRegistry
from ..errors import ConfigError, HL7ParseError, TimestampError
from ..hl7time import parse_hl7_ts
from ..ingest.parser import (
    get_control_id,
    get_facilities,
    get_field,
    get_message_type,
    parse_hl7,
)
from ..ingest.pipeline import (
    SUPPORTED_ORDER_CONTROLS,
    VALID_ACK_CODES,
    VALID_REPORT_STATUSES,
)
from ..msgfiles import display_key, hl7_files
from .rules import KNOWN_RULES, LintFinding, make_finding


def _validate_profile(cfg: HospitalConfig) -> None:
    unknown = set(cfg.lint.disabled_rules) - set(KNOWN_RULES)
    if unknown:
        raise ConfigError(
            f"{cfg.hospital_id}: lint.disabled_rules contains unknown rule ids "
            f"{sorted(unknown)}; known: {sorted(KNOWN_RULES)}"
        )


def _check_ts(
    value: str,
    cfg: HospitalConfig,
    rule_id: str,
    field_name: str,
    control_id: str,
    findings: list[LintFinding],
) -> None:
    """Flag a PRESENT but unparseable timestamp. Absence is judged elsewhere."""
    if not value:
        return
    try:
        parse_hl7_ts(value, default_tz=cfg.tzinfo)
    except TimestampError as exc:
        findings.append(
            make_finding(
                rule_id,
                hospital_id=cfg.hospital_id,
                control_id=control_id,
                field=field_name,
                detail=str(exc),
            )
        )


def lint_text(raw: str, registry: HospitalRegistry) -> list[LintFinding]:
    """Lint one raw HL7 message. Findings for disabled rules are filtered
    out per the matched hospital's profile."""
    try:
        message = parse_hl7(raw)
    except HL7ParseError as exc:
        return [
            make_finding("HL7-001", hospital_id=None, control_id=None, field="", detail=str(exc))
        ]

    findings: list[LintFinding] = []
    control_id = get_control_id(message) or None
    msh4, msh6 = get_facilities(message)
    if not control_id:
        findings.append(make_finding("HL7-002", hospital_id=None, control_id=None, field="MSH-10"))

    cfg = registry.match_facility(msh4, msh6)
    if cfg is None:
        findings.append(
            make_finding(
                "HL7-003",
                hospital_id=None,
                control_id=control_id,
                field="MSH-4",
                detail=f"MSH-4={msh4!r} MSH-6={msh6!r}",
            )
        )
        return findings  # hospital-profile rules cannot apply
    _validate_profile(cfg)
    hosp = cfg.hospital_id

    msg_type = get_message_type(message)
    is_ack = msg_type == "ACK" or msg_type.startswith("ACK^")
    if not is_ack and msg_type not in cfg.expected_message_types:
        findings.append(
            make_finding(
                "HL7-004",
                hospital_id=hosp,
                control_id=control_id,
                field="MSH-9",
                detail=f"{msg_type!r} not in {cfg.expected_message_types}",
            )
        )

    msh7 = get_field(message, "MSH-7")
    if not msh7:
        findings.append(
            make_finding("HL7-006", hospital_id=hosp, control_id=control_id, field="MSH-7")
        )
    else:
        _check_ts(msh7, cfg, "HL7-005", "MSH-7", control_id or "", findings)

    needs_pid = is_ack is False and (
        msg_type.startswith("ADT^") or msg_type in ("ORM^O01", "ORU^R01")
    )
    if needs_pid and not get_field(message, "PID-3"):
        findings.append(
            make_finding("HL7-012", hospital_id=hosp, control_id=control_id, field="PID-3")
        )

    if msg_type in ("ORM^O01", "ORU^R01") and not get_field(message, cfg.accession_field):
        findings.append(
            make_finding(
                "HL7-010",
                hospital_id=hosp,
                control_id=control_id,
                field=cfg.accession_field,
                detail=f"per {hosp} config",
            )
        )

    if msg_type == "ORM^O01":
        if get_field(message, "ORC-1") not in SUPPORTED_ORDER_CONTROLS:
            findings.append(
                make_finding(
                    "HL7-016",
                    hospital_id=hosp,
                    control_id=control_id,
                    field="ORC-1",
                    detail=f"got {get_field(message, 'ORC-1')!r}",
                )
            )
        if not get_field(message, "OBR-4", component=1):
            findings.append(
                make_finding("HL7-011", hospital_id=hosp, control_id=control_id, field="OBR-4")
            )

    if msg_type == "ORU^R01":
        status = get_field(message, "OBR-25")
        if status not in VALID_REPORT_STATUSES:
            findings.append(
                make_finding(
                    "HL7-013",
                    hospital_id=hosp,
                    control_id=control_id,
                    field="OBR-25",
                    detail=f"got {status!r}",
                )
            )
        _check_ts(
            get_field(message, "OBR-22"), cfg, "HL7-014", "OBR-22", control_id or "", findings
        )
        _check_ts(get_field(message, "OBR-7"), cfg, "HL7-015", "OBR-7", control_id or "", findings)

    if is_ack:
        msa1 = get_field(message, "MSA-1")
        msa2 = get_field(message, "MSA-2")
        if msa1 not in VALID_ACK_CODES or not msa2:
            findings.append(
                make_finding(
                    "HL7-020",
                    hospital_id=hosp,
                    control_id=control_id,
                    field="MSA-1",
                    detail=f"MSA-1={msa1!r} MSA-2={msa2!r}",
                )
            )

    disabled = set(cfg.lint.disabled_rules)
    return [f for f in findings if f.rule_id not in disabled]


@dataclass(slots=True)
class LintReport:
    files_checked: int = 0
    by_file: list[tuple[str, list[LintFinding]]] = field(default_factory=list)

    @property
    def findings(self) -> list[LintFinding]:
        return [f for _, group in self.by_file for f in group]

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    def summary(self) -> dict[str, Counter[str]]:
        """hospital id (or '(unattributed)') -> Counter of rule ids."""
        result: dict[str, Counter[str]] = {}
        for f in self.findings:
            result.setdefault(f.hospital_id or "(unattributed)", Counter())[f.rule_id] += 1
        return result


def lint_directory(directory: Path, registry: HospitalRegistry) -> LintReport:
    for cfg in registry.by_id.values():
        _validate_profile(cfg)
    report = LintReport()
    for path in hl7_files(directory):
        # Bytes, then decode: text mode would rewrite bare CR segment ends.
        findings = lint_text(path.read_bytes().decode("utf-8"), registry)
        report.files_checked += 1
        if findings:
            # Forward slashes on every OS so reports and keys match everywhere.
            report.by_file.append((display_key(path, directory), findings))
    return report


def render_lint_text(report: LintReport) -> str:
    lines = [f"linted {report.files_checked} files: {len(report.findings)} finding(s)"]
    for hosp, counts in sorted(report.summary().items()):
        per_rule = "  ".join(f"{rule}={n}" for rule, n in sorted(counts.items()))
        lines.append(f"  {hosp}: {per_rule}")
    for rel_path, findings in report.by_file:
        for f in findings:
            lines.append(
                f"  {rel_path}: [{f.rule_id}/{f.severity}] field={f.field or '-'} {f.message}"
            )
    return "\n".join(lines)
