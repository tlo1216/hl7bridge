"""Sync the store to a FHIR server.

Order matters: Patient, then Encounter and ServiceRequest, then
DiagnosticReport and ImagingStudy, so references always point at
already-upserted resources. Rows that cannot be mapped (no resolvable
subject, a code not in its table) are SKIPPED LOUDLY: counted, listed with
reasons, and surfaced in the rendered report. A sync that skipped rows
never looks like a clean one. Identifier systems come from each hospital's
profile.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from ..config import HospitalRegistry, IdentifierSystems
from ..errors import MappingError
from .client import HapiClient
from .mapper import (
    diagnostic_report_resource,
    encounter_resource,
    imaging_study_resource,
    patient_resource,
    service_request_resource,
)

KINDS = ("Patient", "Encounter", "ServiceRequest", "DiagnosticReport", "ImagingStudy")
NO_SUBJECT = "no resolvable subject patient"

log = logging.getLogger(__name__)


@dataclass(slots=True)
class FhirSyncReport:
    created: Counter[str] = field(default_factory=Counter)
    updated: Counter[str] = field(default_factory=Counter)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, key, reason)

    def total(self, kind: str) -> int:
        return self.created[kind] + self.updated[kind]


def sync_to_fhir(
    conn: sqlite3.Connection, registry: HospitalRegistry, client: HapiClient
) -> FhirSyncReport:
    report = FhirSyncReport()

    def systems(hospital_id: str) -> IdentifierSystems:
        hospital = registry.by_id.get(hospital_id)
        return hospital.systems if hospital else IdentifierSystems.defaults(hospital_id)

    patient_refs: dict[tuple[str, str], str] = {}
    sr_refs: dict[tuple[str, str], str] = {}

    patients = conn.execute(
        "SELECT * FROM patients WHERE merged_into_mrn IS NULL ORDER BY hospital_id, mrn"
    ).fetchall()
    for row in patients:
        try:
            resource = patient_resource(row, systems(row["hospital_id"]))
        except MappingError as exc:
            # The reason names the field and the table, never the value.
            report.skipped.append(("Patient", f"{row['hospital_id']}/{row['mrn']}", str(exc)))
            continue
        server_id, action = client.upsert(resource)
        getattr(report, action)["Patient"] += 1
        patient_refs[(row["hospital_id"], row["mrn"])] = f"Patient/{server_id}"

    for row in conn.execute(
        "SELECT * FROM encounters ORDER BY hospital_id, visit_number"
    ).fetchall():
        key_text = f"{row['hospital_id']}/{row['visit_number']}"
        patient_ref = patient_refs.get((row["hospital_id"], row["patient_mrn"]))
        if patient_ref is None:
            report.skipped.append(("Encounter", key_text, NO_SUBJECT))
            continue
        try:
            encounter = encounter_resource(row, patient_ref, systems(row["hospital_id"]))
        except MappingError as exc:
            report.skipped.append(("Encounter", key_text, str(exc)))
            continue
        _, action = client.upsert(encounter)
        getattr(report, action)["Encounter"] += 1

    for row in conn.execute("SELECT * FROM orders ORDER BY hospital_id, accession").fetchall():
        key = (row["hospital_id"], row["accession"])
        patient_ref = patient_refs.get((row["hospital_id"], row["patient_mrn"] or ""))
        if patient_ref is None:
            report.skipped.append(("ServiceRequest", f"{key[0]}/{key[1]}", NO_SUBJECT))
            continue
        server_id, action = client.upsert(
            service_request_resource(row, patient_ref, systems(row["hospital_id"]))
        )
        getattr(report, action)["ServiceRequest"] += 1
        sr_refs[key] = f"ServiceRequest/{server_id}"

    for row in conn.execute("SELECT * FROM reports ORDER BY hospital_id, accession").fetchall():
        key = (row["hospital_id"], row["accession"])
        patient_ref = patient_refs.get((row["hospital_id"], row["patient_mrn"] or ""))
        if patient_ref is None:
            report.skipped.append(("DiagnosticReport", f"{key[0]}/{key[1]}", NO_SUBJECT))
            continue
        dr = diagnostic_report_resource(
            row,
            patient_ref,
            service_request_ref=sr_refs.get(key),
            systems=systems(row["hospital_id"]),
        )
        _, action = client.upsert(dr)
        getattr(report, action)["DiagnosticReport"] += 1

    for row in conn.execute("SELECT * FROM studies ORDER BY study_uid").fetchall():
        patient_ref = patient_refs.get((row["hospital_id"], row["patient_mrn"] or ""))
        if patient_ref is None:
            report.skipped.append(("ImagingStudy", row["study_uid"], NO_SUBJECT))
            continue
        key = (row["hospital_id"], row["accession"] or "")
        _, action = client.upsert(
            imaging_study_resource(
                row,
                patient_ref,
                service_request_ref=sr_refs.get(key),
                systems=systems(row["hospital_id"]),
            )
        )
        getattr(report, action)["ImagingStudy"] += 1

    # Keys (accessions, study UIDs) stay in the rendered report on stdout.
    # The log carries the kind, the fixed reason phrase and a count only.
    skipped_counts = Counter((kind, reason) for kind, _, reason in report.skipped)
    for (kind, reason), n in sorted(skipped_counts.items()):
        log.warning("fhir sync skipped %d %s row(s): %s", n, kind, reason)
    log.info(
        "fhir sync finished created=%d updated=%d skipped=%d",
        sum(report.created.values()),
        sum(report.updated.values()),
        len(report.skipped),
    )
    return report


def render_sync_text(report: FhirSyncReport) -> str:
    lines = ["FHIR sync results:"]
    for kind in KINDS:
        lines.append(f"  {kind}: created {report.created[kind]}, updated {report.updated[kind]}")
    if report.skipped:
        lines.append(f"  SKIPPED {len(report.skipped)} rows (these need attention):")
        for kind, key, reason in report.skipped:
            lines.append(f"    {kind} {key}: {reason}")
    else:
        lines.append("  skipped: 0")
    return "\n".join(lines)
