"""FHIR mappers: valid R4B resources, exact status maps, ACSN identifiers."""

import sqlite3

import pytest

from hl7bridge.errors import FhirMappingError
from hl7bridge.fhir.mapper import (
    V2_0203,
    diagnostic_report_resource,
    imaging_study_resource,
    patient_resource,
    service_request_resource,
)


def row(**kwargs) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(f"? AS {k}" for k in kwargs)
    result = conn.execute(f"SELECT {cols}", tuple(kwargs.values())).fetchone()
    conn.close()
    return result


def patient_row(**overrides):
    base = {
        "hospital_id": "HOSPA",
        "mrn": "MA1001",
        "family_name": "ZZZTEST",
        "given_name": "ALPHA",
        "birth_date": "1970-01-01",
        "sex": "F",
        "merged_into_mrn": None,
    }
    base.update(overrides)
    return row(**base)


def order_row(**overrides):
    base = {
        "hospital_id": "HOSPA",
        "accession": "ACCA0001",
        "placer_order_id": "PL1",
        "procedure_code": "71250",
        "procedure_description": "CT CHEST WO CONTRAST",
        "code_system": "local",
        "modality": "CT",
        "patient_mrn": "MA1001",
        "ordered_at_utc": "2026-10-07T14:30:00+00:00",
        "status": "new",
    }
    base.update(overrides)
    return row(**base)


def report_row(**overrides):
    base = {
        "hospital_id": "HOSPA",
        "accession": "ACCA0001",
        "control_id": "ORUF0001",
        "status": "F",
        "report_text": "IMPRESSION: No acute findings.",
        "patient_mrn": "MA1001",
        "result_at_utc": "2026-10-07T17:30:00+00:00",
        "received_at_utc": "2026-10-07T17:31:00+00:00",
    }
    base.update(overrides)
    return row(**base)


def study_row(**overrides):
    base = {
        "study_uid": "1.2.826.0.1.3680043.8.498.1",
        "hospital_id": "HOSPA",
        "accession": "ACCA0001",
        "modality": "CT",
        "patient_mrn": "MA1001",
        "study_at_utc": "2026-10-07T15:10:00+00:00",
        "source_file": "x.dcm",
    }
    base.update(overrides)
    return row(**base)


def test_patient_resource():
    p = patient_resource(patient_row())
    assert p.gender == "female"
    assert str(p.birthDate) == "1970-01-01"
    ident = p.identifier[0]
    assert ident.system == "urn:hl7bridge:hospa:mrn"
    assert ident.type.coding[0].code == "MR"
    assert p.name[0].family == "ZZZTEST"


def test_patient_unknown_sex_code_is_declared_unknown():
    assert patient_resource(patient_row(sex="X")).gender == "unknown"
    assert patient_resource(patient_row(sex=None)).gender is None


def test_service_request_accession_is_acsn_identifier():
    sr = service_request_resource(order_row(), "Patient/1")
    assert sr.status == "active"
    assert sr.intent == "order"
    ident = sr.identifier[0]
    assert ident.type.coding[0].system == V2_0203
    assert ident.type.coding[0].code == "ACSN"
    assert ident.value == "ACCA0001"
    assert sr.subject.reference == "Patient/1"
    assert sr.code.coding[0].code == "71250"


def test_cancelled_order_maps_to_revoked():
    assert service_request_resource(order_row(status="cancelled"), "Patient/1").status == "revoked"


def test_unmapped_order_status_raises():
    with pytest.raises(FhirMappingError):
        service_request_resource(order_row(status="weird"), "Patient/1")


@pytest.mark.parametrize(
    ("hl7_status", "fhir_status"),
    [("P", "preliminary"), ("F", "final"), ("C", "corrected")],
)
def test_report_status_mapping(hl7_status, fhir_status):
    dr = diagnostic_report_resource(
        report_row(status=hl7_status),
        "Patient/1",
        service_request_ref="ServiceRequest/2",
        order_row=order_row(),
    )
    assert dr.status == fhir_status
    assert dr.basedOn[0].reference == "ServiceRequest/2"
    assert dr.conclusion == "IMPRESSION: No acute findings."
    assert dr.identifier[0].type.coding[0].code == "ACSN"
    assert dr.code.coding[0].code == "71250"


def test_unknown_report_status_raises_never_guesses():
    with pytest.raises(FhirMappingError, match="refusing to guess"):
        diagnostic_report_resource(
            report_row(status="X"), "Patient/1", service_request_ref=None, order_row=None
        )


def test_report_without_order_gets_generic_code():
    dr = diagnostic_report_resource(
        report_row(), "Patient/1", service_request_ref=None, order_row=None
    )
    assert dr.code.text == "Imaging report"
    assert dr.basedOn is None


def test_imaging_study_identifiers_and_modality():
    s = imaging_study_resource(study_row(), "Patient/1", service_request_ref="ServiceRequest/2")
    systems = {i.system for i in s.identifier}
    assert "urn:dicom:uid" in systems
    assert s.identifier[0].value == "urn:oid:1.2.826.0.1.3680043.8.498.1"
    assert s.modality[0].code == "CT"
    assert s.modality[0].system == "http://dicom.nema.org/resources/ontology/DCM"
    assert s.status == "available"


def test_imaging_study_without_accession_still_valid():
    s = imaging_study_resource(study_row(accession=None), "Patient/1", service_request_ref=None)
    assert len(s.identifier) == 1
    assert s.basedOn is None
