"""Map store rows to FHIR R4B resources.

Conventions (full rationale and deviations in ADR-0003):
- Identifier systems are synthetic URNs per hospital:
  urn:hl7bridge:<hospital>:mrn and urn:hl7bridge:<hospital>:accession;
  DICOM study UIDs use urn:dicom:uid with urn:oid:<uid> values.
- The accession identifier carries HL7 v2 table 0203 type code ACSN, which
  is what links ServiceRequest, DiagnosticReport, and ImagingStudy.
- OBR-25 F/P/C map to final/preliminary/corrected. The ingest pipeline
  quarantines anything else; the mapper still refuses to guess.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fhir.resources.R4B.codeableconcept import CodeableConcept
from fhir.resources.R4B.coding import Coding
from fhir.resources.R4B.diagnosticreport import DiagnosticReport
from fhir.resources.R4B.humanname import HumanName
from fhir.resources.R4B.identifier import Identifier
from fhir.resources.R4B.imagingstudy import ImagingStudy
from fhir.resources.R4B.patient import Patient
from fhir.resources.R4B.reference import Reference
from fhir.resources.R4B.servicerequest import ServiceRequest

from ..errors import FhirMappingError

V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
DCM = "http://dicom.nema.org/resources/ontology/DCM"

_GENDER_MAP = {"F": "female", "M": "male", "O": "other", "U": "unknown"}
_REPORT_STATUS_MAP = {"P": "preliminary", "F": "final", "C": "corrected"}
_ORDER_STATUS_MAP = {"new": "active", "cancelled": "revoked"}


def mrn_system(hospital_id: str) -> str:
    return f"urn:hl7bridge:{hospital_id.lower()}:mrn"


def accession_system(hospital_id: str) -> str:
    return f"urn:hl7bridge:{hospital_id.lower()}:accession"


def mrn_identifier(hospital_id: str, mrn: str) -> Identifier:
    return Identifier(
        type=CodeableConcept(coding=[Coding(system=V2_0203, code="MR")]),
        system=mrn_system(hospital_id),
        value=mrn,
    )


def accession_identifier(hospital_id: str, accession: str) -> Identifier:
    return Identifier(
        type=CodeableConcept(coding=[Coding(system=V2_0203, code="ACSN")]),
        system=accession_system(hospital_id),
        value=accession,
    )


def patient_resource(row: sqlite3.Row) -> Patient:
    sex = row["sex"]
    gender = _GENDER_MAP.get(sex) if sex else None
    if sex and gender is None:
        # Unknown code: declare unknown rather than drop or guess.
        gender = "unknown"
    return Patient(
        identifier=[mrn_identifier(row["hospital_id"], row["mrn"])],
        name=[
            HumanName(
                family=row["family_name"] or None,
                given=[row["given_name"]] if row["given_name"] else None,
            )
        ],
        gender=gender,
        birthDate=row["birth_date"],
    )


def _procedure_concept(row: sqlite3.Row) -> CodeableConcept | None:
    code = row["procedure_code"]
    desc = row["procedure_description"]
    if not code and not desc:
        return None
    coding = None
    if code:
        coding = [
            Coding(
                system=f"urn:hl7bridge:{row['hospital_id'].lower()}:procedure-codes:"
                f"{row['code_system'] or 'local'}",
                code=code,
                display=desc,
            )
        ]
    return CodeableConcept(coding=coding, text=desc or code)


def service_request_resource(row: sqlite3.Row, patient_ref: str) -> ServiceRequest:
    status = _ORDER_STATUS_MAP.get(row["status"])
    if status is None:
        raise FhirMappingError(f"order status {row['status']!r} has no FHIR mapping")
    return ServiceRequest(
        identifier=[accession_identifier(row["hospital_id"], row["accession"])],
        status=status,
        intent="order",
        code=_procedure_concept(row),
        subject=Reference(reference=patient_ref),
        authoredOn=row["ordered_at_utc"],
    )


def diagnostic_report_resource(
    report_row: sqlite3.Row,
    patient_ref: str,
    *,
    service_request_ref: str | None,
    order_row: sqlite3.Row | None,
) -> DiagnosticReport:
    status = _REPORT_STATUS_MAP.get(report_row["status"])
    if status is None:
        raise FhirMappingError(
            f"report status {report_row['status']!r} has no FHIR mapping; refusing to guess"
        )
    code = _procedure_concept(order_row) if order_row is not None else None
    if code is None:
        code = CodeableConcept(text="Imaging report")
    kwargs: dict[str, Any] = {}
    if report_row["result_at_utc"]:
        kwargs["effectiveDateTime"] = report_row["result_at_utc"]
    if service_request_ref is not None:
        kwargs["basedOn"] = [Reference(reference=service_request_ref)]
    return DiagnosticReport(
        identifier=[accession_identifier(report_row["hospital_id"], report_row["accession"])],
        status=status,
        code=code,
        subject=Reference(reference=patient_ref),
        issued=report_row["received_at_utc"],
        conclusion=report_row["report_text"] or None,
        **kwargs,
    )


def imaging_study_resource(
    row: sqlite3.Row, patient_ref: str, *, service_request_ref: str | None
) -> ImagingStudy:
    identifiers = [Identifier(system="urn:dicom:uid", value=f"urn:oid:{row['study_uid']}")]
    if row["accession"]:
        identifiers.append(accession_identifier(row["hospital_id"], row["accession"]))
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
