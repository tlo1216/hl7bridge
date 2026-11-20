"""Map store rows to FHIR R4B resources (the ``fhir sync`` path).

Conventions (full rationale and deviations in ADR-0003 and docs/mapping.md):
- Identifier systems come from the hospital profile's
  ``fhir.identifier_systems`` block, by default
  urn:hl7bridge:<hospital>:mrn, :accession, :placer and :visit. DICOM
  study UIDs use urn:dicom:uid with urn:oid:<uid> values.
- The accession identifier carries HL7 v2 table 0203 type code ACSN, which
  is what links ServiceRequest, DiagnosticReport, and ImagingStudy.
- Codes go through the same tables as ``hl7bridge.v2fhir`` (sex, report
  status, order status, patient class), and a code not in a table raises
  instead of being guessed. ``to-fhir --post`` and ``fhir sync`` therefore
  write the same identifiers and codes.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fhir.resources.R4B.codeableconcept import CodeableConcept
from fhir.resources.R4B.coding import Coding
from fhir.resources.R4B.diagnosticreport import DiagnosticReport
from fhir.resources.R4B.encounter import Encounter
from fhir.resources.R4B.humanname import HumanName
from fhir.resources.R4B.identifier import Identifier
from fhir.resources.R4B.imagingstudy import ImagingStudy
from fhir.resources.R4B.patient import Patient
from fhir.resources.R4B.period import Period
from fhir.resources.R4B.reference import Reference
from fhir.resources.R4B.servicerequest import ServiceRequest

from ..config import IdentifierSystems
from ..errors import FhirMappingError, MappingError
from ..v2fhir.common import procedure_concept, typed_identifier
from ..v2fhir.report import presented_form
from ..v2fhir.tables import (
    ENCOUNTER_STATUS,
    PATIENT_CLASS,
    REPORT_STATUS,
    SEX,
    V2_0203,
)

DCM = "http://dicom.nema.org/resources/ontology/DCM"

# The store keeps 'new' and 'cancelled', written from ORC-1 NW and CA.
_ORDER_STATUS_MAP = {"new": "active", "cancelled": "revoked"}

__all__ = [
    "DCM",
    "V2_0203",
    "accession_identifier",
    "accession_system",
    "diagnostic_report_resource",
    "encounter_resource",
    "imaging_study_resource",
    "mrn_identifier",
    "mrn_system",
    "patient_resource",
    "service_request_resource",
]


def _systems(hospital_id: str, systems: IdentifierSystems | None) -> IdentifierSystems:
    return systems if systems is not None else IdentifierSystems.defaults(hospital_id)


def mrn_system(hospital_id: str) -> str:
    """The default MRN system, for a hospital whose profile sets none."""
    return IdentifierSystems.defaults(hospital_id).mrn


def accession_system(hospital_id: str) -> str:
    """The default accession system, for a hospital whose profile sets none."""
    return IdentifierSystems.defaults(hospital_id).accession


def mrn_identifier(
    hospital_id: str, mrn: str, systems: IdentifierSystems | None = None
) -> Identifier:
    return typed_identifier("MR", _systems(hospital_id, systems).mrn, mrn)


def accession_identifier(
    hospital_id: str, accession: str, systems: IdentifierSystems | None = None
) -> Identifier:
    return typed_identifier("ACSN", _systems(hospital_id, systems).accession, accession)


def patient_resource(row: sqlite3.Row, systems: IdentifierSystems | None = None) -> Patient:
    """A sex code not in table 0001 raises MappingError (PID-8); the sync
    skips that patient and says why."""
    sex = row["sex"]
    gender = SEX.lookup(sex, "PID-8") if sex else None
    given = row["given_name"]
    return Patient(
        identifier=[mrn_identifier(row["hospital_id"], row["mrn"], systems)],
        name=[HumanName(family=row["family_name"] or None, given=[given] if given else None)],
        gender=gender,
        birthDate=row["birth_date"],
    )


def _procedure_concept(row: sqlite3.Row) -> CodeableConcept | None:
    return procedure_concept(
        row["hospital_id"],
        row["code_system"],
        row["procedure_code"] or "",
        row["procedure_description"] or "",
    )


def service_request_resource(
    row: sqlite3.Row, patient_ref: str, systems: IdentifierSystems | None = None
) -> ServiceRequest:
    status = _ORDER_STATUS_MAP.get(row["status"])
    if status is None:
        raise FhirMappingError(f"order status {row['status']!r} has no FHIR mapping")
    resolved = _systems(row["hospital_id"], systems)
    identifiers = [accession_identifier(row["hospital_id"], row["accession"], resolved)]
    if row["placer_order_id"]:
        identifiers.append(typed_identifier("PLAC", resolved.placer, row["placer_order_id"]))
    kwargs: dict[str, Any] = {}
    code = _procedure_concept(row)
    if code is not None:
        kwargs["code"] = code
    return ServiceRequest(
        identifier=identifiers,
        status=status,
        intent="order",
        subject=Reference(reference=patient_ref),
        authoredOn=row["ordered_at_utc"],
        **kwargs,
    )


def diagnostic_report_resource(
    report_row: sqlite3.Row,
    patient_ref: str,
    *,
    service_request_ref: str | None,
    systems: IdentifierSystems | None = None,
) -> DiagnosticReport:
    """The code is the report's own OBR-4, OBR-7 (observed) is the effective
    time and OBR-22 (result) the issued time, as in the IG. The report text
    is the conclusion and a plain-text presentedForm."""
    try:
        status = REPORT_STATUS.lookup(report_row["status"], "OBR-25")
    except MappingError as exc:
        raise FhirMappingError(str(exc)) from None
    code = _procedure_concept(report_row)
    if code is None:
        code = CodeableConcept(text="Imaging report")
    kwargs: dict[str, Any] = {}
    if report_row["observed_at_utc"]:
        kwargs["effectiveDateTime"] = report_row["observed_at_utc"]
    if report_row["result_at_utc"]:
        kwargs["issued"] = report_row["result_at_utc"]
    if service_request_ref is not None:
        kwargs["basedOn"] = [Reference(reference=service_request_ref)]
    text = report_row["report_text"]
    if text:
        kwargs["conclusion"] = text
        kwargs["presentedForm"] = [presented_form(text)]
    return DiagnosticReport(
        identifier=[
            accession_identifier(report_row["hospital_id"], report_row["accession"], systems)
        ],
        status=status,
        code=code,
        subject=Reference(reference=patient_ref),
        **kwargs,
    )


def encounter_resource(
    row: sqlite3.Row, patient_ref: str, systems: IdentifierSystems | None = None
) -> Encounter:
    """A stored visit (PV1). A patient class not in table 0004 raises
    MappingError (PV1-2); the sync skips that visit and says why."""
    resolved = _systems(row["hospital_id"], systems)
    system, code = PATIENT_CLASS.lookup(row["patient_class"], "PV1-2").split("|", 1)
    end = row["discharged_at_utc"]
    status = "finished" if end else ENCOUNTER_STATUS.lookup(row["patient_class"], "PV1-2")
    # "class" is a Python keyword; the model takes it by its FHIR name.
    kwargs: dict[str, Any] = {"class": Coding(system=system, code=code)}
    if row["admitted_at_utc"] or end:
        kwargs["period"] = Period(start=row["admitted_at_utc"], end=end)
    return Encounter(
        identifier=[typed_identifier("VN", resolved.visit, row["visit_number"])],
        status=status,
        subject=Reference(reference=patient_ref),
        **kwargs,
    )


def imaging_study_resource(
    row: sqlite3.Row,
    patient_ref: str,
    *,
    service_request_ref: str | None,
    systems: IdentifierSystems | None = None,
) -> ImagingStudy:
    identifiers = [Identifier(system="urn:dicom:uid", value=f"urn:oid:{row['study_uid']}")]
    if row["accession"]:
        identifiers.append(accession_identifier(row["hospital_id"], row["accession"], systems))
    kwargs: dict[str, Any] = {}
    if service_request_ref is not None:
        kwargs["basedOn"] = [Reference(reference=service_request_ref)]
    return ImagingStudy(
        identifier=identifiers,
        status="available",
        modality=[Coding(system=DCM, code=row["modality"])],
        subject=Reference(reference=patient_ref),
        started=row["study_at_utc"],
        **kwargs,
    )
