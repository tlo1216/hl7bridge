"""FHIR mappers: valid R4B resources, exact status maps, ACSN identifiers."""

import base64
import sqlite3

import pytest

from hl7bridge.config import IdentifierSystems
from hl7bridge.errors import FhirMappingError, MappingError
from hl7bridge.fhir.mapper import (
    V2_0203,
    diagnostic_report_resource,
    encounter_resource,
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
        "procedure_code": "71250",
        "procedure_description": "CT CHEST WO CONTRAST",
        "code_system": "local",
        "patient_mrn": "MA1001",
        "observed_at_utc": "2026-10-07T15:10:00+00:00",
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


@pytest.mark.parametrize(
    ("code", "gender"),
    [
        ("F", "female"),
        ("M", "male"),
        ("O", "other"),
        ("U", "unknown"),
        ("A", "other"),
        ("N", "other"),
    ],
)
def test_patient_sex_follows_table_0001(code, gender):
    assert patient_resource(patient_row(sex=code)).gender == gender


def test_patient_unknown_sex_code_is_refused_never_guessed():
    with pytest.raises(MappingError, match="PID-8: code not in table 0001") as info:
        patient_resource(patient_row(sex="X"))
    assert "X" not in str(info.value).replace("PID-8", "")
    assert patient_resource(patient_row(sex=None)).gender is None


def test_profile_identifier_systems_are_used():
    systems = IdentifierSystems(
        mrn="http://example.test/mrn",
        accession="http://example.test/acc",
        placer="http://example.test/placer",
        visit="http://example.test/visit",
        observation="http://example.test/obs",
    )
    assert patient_resource(patient_row(), systems).identifier[0].system == systems.mrn
    sr = service_request_resource(order_row(), "Patient/1", systems)
    assert [i.system for i in sr.identifier] == [systems.accession, systems.placer]


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
    placer = sr.identifier[1]
    assert (placer.type.coding[0].code, placer.value) == ("PLAC", "PL1")
    assert sr.authoredOn.isoformat() == "2026-10-07T14:30:00+00:00"


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
    )
    assert dr.status == fhir_status
    assert dr.basedOn[0].reference == "ServiceRequest/2"
    assert dr.conclusion == "IMPRESSION: No acute findings."
    assert dr.identifier[0].type.coding[0].code == "ACSN"
    assert dr.code.coding[0].code == "71250"
    # OBR-7 is the effective time and OBR-22 the issued time, as in the IG.
    assert dr.effectiveDateTime.isoformat() == "2026-10-07T15:10:00+00:00"
    assert dr.issued.isoformat() == "2026-10-07T17:30:00+00:00"
    form = dr.presentedForm[0]
    assert form.contentType == "text/plain; charset=utf-8"
    data = form.model_dump(mode="json")["data"]
    assert base64.b64decode(data).decode("utf-8") == "IMPRESSION: No acute findings."


def test_unknown_report_status_raises_never_guesses():
    with pytest.raises(FhirMappingError, match="refusing to guess"):
        diagnostic_report_resource(report_row(status="Q"), "Patient/1", service_request_ref=None)


def test_report_without_a_code_gets_generic_code():
    dr = diagnostic_report_resource(
        report_row(procedure_code=None, procedure_description=None),
        "Patient/1",
        service_request_ref=None,
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


def encounter_row(**overrides):
    base = {
        "hospital_id": "HOSPA",
        "visit_number": "V1",
        "patient_mrn": "MA1001",
        "patient_class": "I",
        "admitted_at_utc": "2026-10-07T14:00:00+00:00",
        "discharged_at_utc": None,
        "last_message_id": 1,
    }
    base.update(overrides)
    return row(**base)


def test_encounter_resource():
    enc = encounter_resource(encounter_row(), "Patient/1")
    assert enc.identifier[0].type.coding[0].code == "VN"
    assert enc.identifier[0].system == "urn:hl7bridge:hospa:visit"
    assert (enc.class_fhir.code, enc.status) == ("IMP", "in-progress")
    assert enc.period.start.isoformat() == "2026-10-07T14:00:00+00:00"


def test_discharged_encounter_is_finished():
    enc = encounter_resource(
        encounter_row(discharged_at_utc="2026-10-08T10:00:00+00:00"), "Patient/1"
    )
    assert enc.status == "finished"
    assert enc.period.end.isoformat() == "2026-10-08T10:00:00+00:00"


def test_encounter_unknown_patient_class_is_refused():
    with pytest.raises(MappingError, match="PV1-2"):
        encounter_resource(encounter_row(patient_class="Z"), "Patient/1")
    with pytest.raises(MappingError, match="PV1-2"):
        encounter_resource(encounter_row(patient_class=""), "Patient/1")
