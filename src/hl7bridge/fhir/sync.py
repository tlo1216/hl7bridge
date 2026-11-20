"""Sync the store to a FHIR server.

Patient, Encounter, ServiceRequest, Observation and DiagnosticReport are
rebuilt from the stored messages (``messages.raw``) by the same mapping and
the same post as ``to-fhir --post`` (``hl7bridge.v2fhir`` and
``fhir/post.py``), so the two paths write identical content and never undo
each other:

every accepted ADT^A01, A04, A08, A40, ORM^O01 and ORU^R01, in one pass in the
order they were received (``messages.id``), as ``to-fhir --post`` would post
them. Only ADT changes a Patient, and ORM and ORU create it when it is
missing, so the order across message types decides which message creates
it: keeping the received order keeps the demographics a direct post keeps.
For each stored report only the ORU it came from (``reports.message_id``,
the latest version the store accepted) is posted whole. Any other ORU (an
older version) posts its Patient entry only, at its place in the order, so
it creates the Patient exactly when a direct post of it would have.

ImagingStudy has no message; it is still built from the ``studies`` rows,
and refers to the Patient and ServiceRequest posted above.

ADT^A40 is posted like any other message, in its place in the order: the
surviving Patient is updated and the merged-away one is set inactive with a
``replaced-by`` link (a bare stub when the server has none). The merged-away
patient's own messages are posted too, exactly as ``to-fhir --post`` posts
them, so the two paths agree; nothing is moved onto the survivor. Chains
(A to B, then B to C) are posted edge by edge. An A40 that the server's
current state contradicts (the merged-away Patient already replaced by
another, or a survivor that is itself merged away), a message that cannot
be mapped (a code not in its table, say), and a report older than the
version the server holds are the three reasons a message is SKIPPED LOUDLY:
counted, listed with the field and reason, and surfaced in the rendered
report. A sync that skipped anything never
looks like a clean one. Identifier systems come from each hospital's
profile.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from ..config import HospitalRegistry, IdentifierSystems
from ..errors import FhirMappingError, MappingError
from ..v2fhir import message_to_bundle
from ..v2fhir.bundle import ADT_EVENTS, MERGE_EVENT, patient_only
from .client import HapiClient
from .mapper import imaging_study_resource
from .post import MergeRefusedError, StaleResultError, post_mapped

KINDS = (
    "Patient",
    "Encounter",
    "ServiceRequest",
    "Observation",
    "DiagnosticReport",
    "ImagingStudy",
)
NO_SUBJECT = "no resolvable subject patient"
STALE = "the server holds a newer version of this report; not posted"
ORDER_TYPE = "ORM^O01"
REPORT_TYPE = "ORU^R01"

log = logging.getLogger(__name__)


@dataclass(slots=True)
class FhirSyncReport:
    created: Counter[str] = field(default_factory=Counter)
    updated: Counter[str] = field(default_factory=Counter)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, key, reason)
    retired: int = 0  # Observations set to entered-in-error by a correction

    def total(self, kind: str) -> int:
        return self.created[kind] + self.updated[kind]


def sync_to_fhir(
    conn: sqlite3.Connection, registry: HospitalRegistry, client: HapiClient
) -> FhirSyncReport:
    report = FhirSyncReport()
    # (kind, key) -> "created" or "updated"; a key created once stays created.
    outcome: dict[tuple[str, str], str] = {}
    patient_ids: dict[tuple[str, str], str] = {}
    order_ids: dict[tuple[str, str], str] = {}

    types = (*ADT_EVENTS, MERGE_EVENT, ORDER_TYPE, REPORT_TYPE)
    marks = ", ".join("?" for _ in types)
    rows = conn.execute(
        f"SELECT id, hospital_id, message_type, raw FROM messages"  # the marks are fixed "?" only
        f" WHERE message_type IN ({marks}) ORDER BY id",
        types,
    ).fetchall()
    current = {int(r["message_id"]) for r in conn.execute("SELECT message_id FROM reports")}

    for row in rows:
        key_text = f"{row['hospital_id']}/message {row['id']}"
        try:
            mapped = message_to_bundle(str(row["raw"]), registry)
        except MappingError as exc:
            # The reason names the field and the table, never the value.
            report.skipped.append((row["message_type"], key_text, str(exc)))
            continue
        if mapped.message_type == REPORT_TYPE and int(row["id"]) not in current:
            mapped = patient_only(mapped)
        try:
            committed = post_mapped(client, mapped)
        except StaleResultError:
            report.skipped.append((row["message_type"], key_text, STALE))
            continue
        except MergeRefusedError as exc:
            report.skipped.append((row["message_type"], key_text, str(exc.reason)))
            continue
        for done in committed:
            if done.retired:
                report.retired += 1
                continue
            slot = (done.kind, done.key)
            if done.code == 201 or outcome.get(slot) == "created":
                outcome[slot] = "created"
            else:
                outcome[slot] = "updated"
            value = done.key.partition("|")[2]
            if done.server_id and done.kind == "Patient":
                patient_ids[(mapped.hospital_id, value)] = done.server_id
            elif done.server_id and done.kind == "ServiceRequest":
                order_ids[(mapped.hospital_id, value)] = done.server_id

    def systems(hospital_id: str) -> IdentifierSystems:
        hospital = registry.by_id.get(hospital_id)
        return hospital.systems if hospital else IdentifierSystems.defaults(hospital_id)

    def server_id(
        kind: str, ids: dict[tuple[str, str], str], hospital_id: str, value: str
    ) -> str | None:
        if (hospital_id, value) in ids:
            return ids[(hospital_id, value)]
        system = systems(hospital_id).mrn if kind == "Patient" else systems(hospital_id).accession
        found = client.search_identifier(kind, system, value)
        if len(found) == 1:
            ids[(hospital_id, value)] = str(found[0]["id"])
            return ids[(hospital_id, value)]
        return None

    for row in conn.execute("SELECT * FROM studies ORDER BY study_uid").fetchall():
        mrn = row["patient_mrn"] or ""
        if not mrn:
            report.skipped.append(("ImagingStudy", row["study_uid"], NO_SUBJECT))
            continue
        try:
            patient_id = server_id("Patient", patient_ids, row["hospital_id"], mrn)
            accession = row["accession"] or ""
            order_id = (
                server_id("ServiceRequest", order_ids, row["hospital_id"], accession)
                if accession
                else None
            )
            if patient_id is None:
                report.skipped.append(("ImagingStudy", row["study_uid"], NO_SUBJECT))
                continue
            study = imaging_study_resource(
                row,
                f"Patient/{patient_id}",
                service_request_ref=f"ServiceRequest/{order_id}" if order_id else None,
                systems=systems(row["hospital_id"]),
            )
            _, action = client.upsert(study)
        except (MappingError, FhirMappingError) as exc:
            report.skipped.append(("ImagingStudy", row["study_uid"], str(exc)))
            continue
        outcome[("ImagingStudy", row["study_uid"])] = action

    for (kind, _), action in outcome.items():
        getattr(report, action)[kind] += 1

    # Keys (accessions, study UIDs) stay in the rendered report on stdout.
    # The log carries the kind, the fixed reason phrase and a count only.
    skipped_counts = Counter((kind, reason) for kind, _, reason in report.skipped)
    for (kind, reason), n in sorted(skipped_counts.items()):
        log.warning("fhir sync skipped %d %s row(s): %s", n, kind, reason)
    log.info(
        "fhir sync finished created=%d updated=%d retired=%d skipped=%d",
        sum(report.created.values()),
        sum(report.updated.values()),
        report.retired,
        len(report.skipped),
    )
    return report


def render_sync_text(report: FhirSyncReport) -> str:
    lines = ["FHIR sync results:"]
    for kind in KINDS:
        lines.append(f"  {kind}: created {report.created[kind]}, updated {report.updated[kind]}")
    if report.retired:
        lines.append(f"  Observations retired by a correction: {report.retired}")
    if report.skipped:
        lines.append(f"  SKIPPED {len(report.skipped)} rows (these need attention):")
        for kind, key, reason in report.skipped:
            lines.append(f"    {kind} {key}: {reason}")
    else:
        lines.append("  skipped: 0")
    return "\n".join(lines)
