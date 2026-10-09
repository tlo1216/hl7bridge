"""Sync the store to a FHIR server.

Order matters: Patient, then ServiceRequest, then DiagnosticReport and
ImagingStudy, so references always point at already-upserted resources.
Rows that cannot be mapped (no resolvable subject) are SKIPPED LOUDLY:
counted, listed with reasons, and surfaced in the rendered report. A sync
that skipped rows never looks like a clean one.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from ..config import HospitalRegistry
from .client import HapiClient
from .mapper import (
    diagnostic_report_resource,
    imaging_study_resource,
    patient_resource,
    service_request_resource,
)

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
    patient_refs: dict[tuple[str, str], str] = {}
    sr_refs: dict[tuple[str, str], str] = {}
    orders_by_key: dict[tuple[str, str], sqlite3.Row] = {}

    patients = conn.execute(
        "SELECT * FROM patients WHERE merged_into_mrn IS NULL ORDER BY hospital_id, mrn"
    ).fetchall()
    for row in patients:
        resource = patient_resource(row)
        server_id, action = client.upsert(resource)
        getattr(report, action)["Patient"] += 1
        patient_refs[(row["hospital_id"], row["mrn"])] = f"Patient/{server_id}"

    for row in conn.execute("SELECT * FROM orders ORDER BY hospital_id, accession").fetchall():
        key = (row["hospital_id"], row["accession"])
        orders_by_key[key] = row
        patient_ref = patient_refs.get((row["hospital_id"], row["patient_mrn"] or ""))
        if patient_ref is None:
            report.skipped.append(
                ("ServiceRequest", f"{key[0]}/{key[1]}", "no resolvable subject patient")
            )
            continue
        server_id, action = client.upsert(service_request_resource(row, patient_ref))
        getattr(report, action)["ServiceRequest"] += 1
        sr_refs[key] = f"ServiceRequest/{server_id}"

    for row in conn.execute("SELECT * FROM reports ORDER BY hospital_id, accession").fetchall():
        key = (row["hospital_id"], row["accession"])
        patient_ref = patient_refs.get((row["hospital_id"], row["patient_mrn"] or ""))
        if patient_ref is None:
            report.skipped.append(
                ("DiagnosticReport", f"{key[0]}/{key[1]}", "no resolvable subject patient")
            )
            continue
        dr = diagnostic_report_resource(
            row,
            patient_ref,
            service_request_ref=sr_refs.get(key),
            order_row=orders_by_key.get(key),
        )
        _, action = client.upsert(dr)
        getattr(report, action)["DiagnosticReport"] += 1

    for row in conn.execute("SELECT * FROM studies ORDER BY study_uid").fetchall():
        patient_ref = patient_refs.get((row["hospital_id"], row["patient_mrn"] or ""))
        if patient_ref is None:
            report.skipped.append(
                ("ImagingStudy", row["study_uid"], "no resolvable subject patient")
            )
            continue
        key = (row["hospital_id"], row["accession"] or "")
        _, action = client.upsert(
            imaging_study_resource(row, patient_ref, service_request_ref=sr_refs.get(key))
        )
        getattr(report, action)["ImagingStudy"] += 1

    for kind, skipped_key, reason in report.skipped:
        log.warning("fhir sync skipped %s %s: %s", kind, skipped_key, reason)
    return report


def render_sync_text(report: FhirSyncReport) -> str:
    kinds = ("Patient", "ServiceRequest", "DiagnosticReport", "ImagingStudy")
    lines = ["FHIR sync results:"]
    for kind in kinds:
        lines.append(f"  {kind}: created {report.created[kind]}, updated {report.updated[kind]}")
    if report.skipped:
        lines.append(f"  SKIPPED {len(report.skipped)} rows (these need attention):")
        for kind, key, reason in report.skipped:
            lines.append(f"    {kind} {key}: {reason}")
    else:
        lines.append("  skipped: 0")
    return "\n".join(lines)
