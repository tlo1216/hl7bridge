# v2 to FHIR mapping

Generated from `src/hl7bridge/v2fhir/spec.py` by
`python tools/gen_mapping_doc.py`. Do not edit by hand: a test fails
when this file and the spec differ, and another fails when a mapper
reads a field that has no row here.

The IG column names a page of the HL7 v2-to-FHIR implementation guide, 1.0.0 (STU 1), at https://hl7.org/fhir/uv/v2mappings/. The guide is cited by page; its tables are
not copied. The guide has a message map for ADT^A01 and none for A04
or A08; hl7bridge maps all three with the same PID and PV1 rows. It has
no map for ADT^A40 and none from MRG to Patient (only MRG to Account);
the merged-away Patient and its `replaced-by` link are hl7bridge's own
reading of FHIR's Patient.link, not an IG map.

Resources are FHIR R4B models (`fhir.resources`); servers are R4.
Identifier systems come from the hospital profile (`fhir.identifier_systems`).

## Messages

| Message | Resources |
| --- | --- |
| ADT^A01 | Patient, Encounter (when PV1-19 is valued) |
| ADT^A04 | Patient, Encounter (when PV1-19 is valued) |
| ADT^A08 | Patient, Encounter (when PV1-19 is valued) |
| ADT^A40 | Patient (survivor), Patient (merged away: inactive, linked replaced-by) |
| ORM^O01 | Patient (created when missing), ServiceRequest |
| ORU^R01 | Patient (created when missing), Observation per structured OBX, DiagnosticReport |

Each resource is keyed on its first identifier, inside one transaction
Bundle. Observation and DiagnosticReport are conditional updates. Patient
from ADT, Encounter and ServiceRequest are conditional updates that a post
first merges into the current resource, because an empty v2 field means
no change. Patient from ORM and ORU is a conditional create: only ADT
writes the Patient. Anything not listed is refused.

## `message`

| v2 | FHIR | IG map | Notes |
| --- | --- | --- | --- |
| MSH-4 | (routing) | none | Sending facility, matched to a hospital profile. |
| MSH-6 | (routing) | none | Receiving facility, tried when MSH-4 matches none. |
| MSH-9 | (routing) | none | Message type and trigger. Must be one hl7bridge maps and one the profile lists. |
| MSH-7 | (time zone) | none | Its UTC offset, when it has one, is the zone of every time in the message that has none, as HL7 v2 defines it. Without one the hospital profile's zone is used. |

## `pid_to_patient`

| v2 | FHIR | IG map | Notes |
| --- | --- | --- | --- |
| PID-3 | Patient.identifier (MR) | [ConceptMap-segment-pid-to-patient](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pid-to-patient.html) | Component 1 of the MRN repetition, with the profile's mrn system: the first repetition when none has a type code (PID-3.5), else the first one typed MR; a typed list with no MR is refused. The key; only ADT writes the Patient, and ORM and ORU create it when missing. The IG maps every repetition and takes the system from the assigning authority; hl7bridge reads one system per hospital. |
| PID-5 | Patient.name | [ConceptMap-segment-pid-to-patient](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pid-to-patient.html) | First repetition: component 1 family, components 2 and 3 given. |
| PID-7 | Patient.birthDate | [ConceptMap-segment-pid-to-patient](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pid-to-patient.html) | Date part only. The IG adds a birthTime extension for a longer value; not written. |
| PID-8 | Patient.gender | [ConceptMap-segment-pid-to-patient](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pid-to-patient.html) | Through table 0001. |
| PID-11 | Patient.address | [ConceptMap-segment-pid-to-patient](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pid-to-patient.html) | First repetition, components 1 to 6 (lines, city, state, postal code, country). |
| PID-13 | Patient.telecom | [ConceptMap-segment-pid-to-patient](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pid-to-patient.html) | First repetition. Component 4 as email when component 3 is Internet, else component 1 as phone. Use is home, as the IG says for PID-13. |

## `mrg_to_patient`

| v2 | FHIR | IG map | Notes |
| --- | --- | --- | --- |
| MRG-1 | Patient.identifier (MR), Patient.active, Patient.link | none | ADT^A40 only. Component 1 is the prior MRN, with the profile's MRN system (component 5, when valued, must be MR; component 4, when valued on both MRG-1 and PID-3, must match). One PID, one MRG and one repetition; the same MRN as PID-3, an empty or HL7-null MRN are refused. The Patient is set inactive and linked replaced-by to the survivor (PID). The IG (1.0.0) has no MRG-to-Patient map and no ADT_A40 message map; its only MRG map, segment-mrg-to-account, targets Account and leaves MRG-1 unmapped. A post merges this into the current Patient (existing links stay, a contradicting or reversed merge is refused), or writes a bare inactive one when the server has none; the offline Bundle makes it a conditional create. The survivor gets no link. |

## `pv1_to_encounter`

| v2 | FHIR | IG map | Notes |
| --- | --- | --- | --- |
| PV1-2 | Encounter.class, Encounter.status | [ConceptMap-segment-pv1-to-encounter](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pv1-to-encounter.html) | Class through table 0004 to v3 ActCode (R, B, C, N, U stay v2-0004 codes). Status through the table 0004 status map when PV1-45 is empty. |
| PV1-19 | Encounter.identifier (VN) | [ConceptMap-segment-pv1-to-encounter](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pv1-to-encounter.html) | Component 1 with the profile's visit system; the key. No visit number, no Encounter. |
| PV1-44 | Encounter.period.start | [ConceptMap-segment-pv1-to-encounter](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pv1-to-encounter.html) | A time with no offset takes MSH-7's offset, else the hospital's zone; a date with no time stays a date. |
| PV1-45 | Encounter.period.end | [ConceptMap-segment-pv1-to-encounter](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-pv1-to-encounter.html) | When valued, Encounter.status is finished, as in the IG. A post merges the Encounter into the current one, so an empty PV1-44 or PV1-45 changes nothing. |

## `orc_obr_to_service_request`

| v2 | FHIR | IG map | Notes |
| --- | --- | --- | --- |
| ORC-1 | ServiceRequest.status | [ConceptMap-segment-orc-to-servicerequest](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-orc-to-servicerequest.html) | NW active, CA revoked; any other order control is refused. hl7bridge's own table, not the IG's table 0119 map. |
| ORC-2 | ServiceRequest.identifier (PLAC) | [ConceptMap-segment-orc-to-servicerequest](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-orc-to-servicerequest.html) | Placer order number, with the profile's placer system. The IG prefers OBR-2 and falls back to ORC-2; hl7bridge reads ORC-2 first, as the store does. |
| OBR-2 | ServiceRequest.identifier (PLAC) | [ConceptMap-segment-orc-to-servicerequest](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-orc-to-servicerequest.html) | Used when ORC-2 is empty. |
| OBR-2 | ServiceRequest.identifier (ACSN) | none | Whichever of OBR-2, OBR-3, ORC-2, ORC-3 the profile names in accession_field. Written as an identifier with type ACSN and the profile's accession system, and used as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier; hl7bridge keys imaging on the accession, wherever the site puts it. |
| OBR-3 | ServiceRequest.identifier (ACSN) | none | Whichever of OBR-2, OBR-3, ORC-2, ORC-3 the profile names in accession_field. Written as an identifier with type ACSN and the profile's accession system, and used as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier; hl7bridge keys imaging on the accession, wherever the site puts it. |
| ORC-2 | ServiceRequest.identifier (ACSN) | none | Whichever of OBR-2, OBR-3, ORC-2, ORC-3 the profile names in accession_field. Written as an identifier with type ACSN and the profile's accession system, and used as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier; hl7bridge keys imaging on the accession, wherever the site puts it. |
| ORC-3 | ServiceRequest.identifier (ACSN) | none | Whichever of OBR-2, OBR-3, ORC-2, ORC-3 the profile names in accession_field. Written as an identifier with type ACSN and the profile's accession system, and used as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier; hl7bridge keys imaging on the accession, wherever the site puts it. |
| OBR-4 | ServiceRequest.code | none | Components 1 and 2. The coding system comes from the profile's procedure_code_system, not component 3, so both paths write one code. |
| ORC-9 | ServiceRequest.authoredOn | [ConceptMap-segment-orc-to-servicerequest](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-orc-to-servicerequest.html) | Transaction time, for a new order (NW) only; a cancel leaves the time the order was written. A time with no offset takes MSH-7's offset, else the hospital's zone; a date with no time stays a date. |
| MSH-7 | ServiceRequest.authoredOn | none | Used when ORC-9 is empty, as the store does (NW only). |

## `obr_to_diagnostic_report`

| v2 | FHIR | IG map | Notes |
| --- | --- | --- | --- |
| OBR-2 | DiagnosticReport.identifier (ACSN) | none | Whichever of OBR-2, OBR-3, ORC-2, ORC-3 the profile names in accession_field. Written as an identifier with type ACSN and the profile's accession system, and used as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier; hl7bridge keys imaging on the accession, wherever the site puts it. |
| OBR-3 | DiagnosticReport.identifier (ACSN) | none | Whichever of OBR-2, OBR-3, ORC-2, ORC-3 the profile names in accession_field. Written as an identifier with type ACSN and the profile's accession system, and used as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier; hl7bridge keys imaging on the accession, wherever the site puts it. |
| ORC-2 | DiagnosticReport.identifier (ACSN) | none | Whichever of OBR-2, OBR-3, ORC-2, ORC-3 the profile names in accession_field. Written as an identifier with type ACSN and the profile's accession system, and used as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier; hl7bridge keys imaging on the accession, wherever the site puts it. |
| ORC-3 | DiagnosticReport.identifier (ACSN) | none | Whichever of OBR-2, OBR-3, ORC-2, ORC-3 the profile names in accession_field. Written as an identifier with type ACSN and the profile's accession system, and used as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier; hl7bridge keys imaging on the accession, wherever the site puts it. |
| OBR-2 | DiagnosticReport.basedOn | none | A logical reference to the ServiceRequest by its accession identifier (type ServiceRequest, identifier ACSN). The ORU does not write the order, and no server lookup is needed. |
| OBR-3 | DiagnosticReport.basedOn | none | A logical reference to the ServiceRequest by its accession identifier (type ServiceRequest, identifier ACSN). The ORU does not write the order, and no server lookup is needed. |
| ORC-2 | DiagnosticReport.basedOn | none | A logical reference to the ServiceRequest by its accession identifier (type ServiceRequest, identifier ACSN). The ORU does not write the order, and no server lookup is needed. |
| ORC-3 | DiagnosticReport.basedOn | none | A logical reference to the ServiceRequest by its accession identifier (type ServiceRequest, identifier ACSN). The ORU does not write the order, and no server lookup is needed. |
| OBR-4 | DiagnosticReport.code | [ConceptMap-segment-obr-to-diagnosticreport](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obr-to-diagnosticreport.html) | As for ServiceRequest.code. No code gives the text Imaging report. |
| OBR-7 | DiagnosticReport.effectiveDateTime | [ConceptMap-segment-obr-to-diagnosticreport](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obr-to-diagnosticreport.html) | OBR-8 is not read, so no effectivePeriod is written. A time with no offset takes MSH-7's offset, else the hospital's zone; a date with no time stays a date. |
| OBR-22 | DiagnosticReport.issued | [ConceptMap-segment-obr-to-diagnosticreport](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obr-to-diagnosticreport.html) | An instant: a date with no time is refused, never padded to midnight. |
| OBR-25 | DiagnosticReport.status | [ConceptMap-segment-obr-to-diagnosticreport](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obr-to-diagnosticreport.html) | Through table 0123. Required. |
| OBX-2 | DiagnosticReport.conclusion | none | TX and FT mark an OBX as narrative: report text, not a result. |
| OBX-5 | DiagnosticReport.conclusion, DiagnosticReport.presentedForm | [ConceptMap-segment-obr-to-diagnosticreport](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obr-to-diagnosticreport.html) | The narrative OBX lines, every repetition read whole, joined with line breaks; the same text as a text/plain presentedForm. No Observation per line. |
| (derived) | DiagnosticReport.result | [ConceptMap-segment-obr-to-diagnosticreport](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obr-to-diagnosticreport.html) | One reference per structured OBX. A post of a corrected report sets each Observation the report no longer carries to entered-in-error. |

## `obx_to_observation`

| v2 | FHIR | IG map | Notes |
| --- | --- | --- | --- |
| OBX-1 | Observation.identifier | none | Set id, the last part of the key <accession>/<OBX-3.1>/set/<OBX-1> when OBX-4 is empty. |
| OBX-2 | Observation.value[x] | [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html) | Chooses the value element through the value type table. Required when OBX-5 has a value; an unknown type is refused. TX and FT are narrative and not mapped here. |
| OBX-3 | Observation.code | [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html) | CE or CWE. Component 1 is part of the key. |
| OBX-4 | Observation.identifier | none | Sub-id, the last part of the key <accession>/<OBX-3.1>/sub/<OBX-4>. Key parts may not hold / or a FHIR search special character. |
| OBX-5 | Observation.value[x] | [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html) | NM must be a plain number, its digits kept as sent (12.50 stays 12.50); ST joins repetitions, each read whole; other types may not repeat. |
| OBX-6 | Observation.valueQuantity unit | [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html) | UCUM system and code only when component 3 is UCUM; otherwise the unit text alone. |
| OBX-7 | Observation.referenceRange.text | [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html) |  |
| OBX-8 | Observation.interpretation | [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html) | Every repetition, through table 0078. |
| OBX-11 | Observation.status | [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html) | Through table 0085. Required; codes the IG map leaves unmapped are refused. |
| OBX-14 | Observation.effectiveDateTime | [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html) | A time with no offset takes MSH-7's offset, else the hospital's zone; a date with no time stays a date. |
| NTE-3 | Observation.note.text | [ConceptMap-segment-nte-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-nte-to-observation.html) | Each NTE after a structured OBX, repetitions read whole and joined. An NTE before the first OBX, or after a narrative one, is not mapped. |

## Code tables

A code that is not listed is refused with the field position. Codes
only; the code meanings are in the HL7 tables themselves.

### Table 0001, administrative sex, to Patient.gender

IG map: [ConceptMap-table-hl70001-to-administrative-gender](https://hl7.org/fhir/uv/v2mappings/ConceptMap-table-hl70001-to-administrative-gender.html)

`F` female, `M` male, `O` other, `U` unknown, `A` other, `N` other

### Table 0004, patient class, to Encounter.class

IG map: [ConceptMap-table-hl70004-to-v3-actcode](https://hl7.org/fhir/uv/v2mappings/ConceptMap-table-hl70004-to-v3-actcode.html)

`E` EMER, `I` IMP, `O` AMB, `P` PRENC, `R` R, `B` B, `C` C, `N` N, `U` U

### Table 0004, patient class, to Encounter.status (when PV1-45 is empty)

IG map: [ConceptMap-table-hl70004-to-encounter-status](https://hl7.org/fhir/uv/v2mappings/ConceptMap-table-hl70004-to-encounter-status.html)

`E` in-progress, `I` in-progress, `O` in-progress, `P` planned, `R` in-progress, `B` in-progress, `C` in-progress, `N` in-progress, `U` unknown

### Table 0085, observation result status, to Observation.status

IG map: [ConceptMap-table-hl70085-to-observation-status](https://hl7.org/fhir/uv/v2mappings/ConceptMap-table-hl70085-to-observation-status.html)

`A` amended, `C` corrected, `D` entered-in-error, `F` final, `P` preliminary, `W` entered-in-error, `X` cancelled

### Table 0123, result status, to DiagnosticReport.status

IG map: [ConceptMap-table-hl70123-queries-to-diagnostic-report-status](https://hl7.org/fhir/uv/v2mappings/ConceptMap-table-hl70123-queries-to-diagnostic-report-status.html)

`O` registered, `I` registered, `S` registered, `P` preliminary, `C` corrected, `R` partial, `F` final, `X` cancelled

### Table 0078, interpretation codes, to Observation.interpretation

IG map: [ConceptMap-table-hl70078-to-v3-observationinterpretation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-table-hl70078-to-v3-observationinterpretation.html)

`<` <, `>` >, `A` A, `AA` AA, `B` B, `CAR` CAR, `D` D, `DET` DET, `E` E, `EX` EX, `EXP` EXP, `H` H, `HH` HH, `HU` HU, `I` I, `IE` IE, `IND` IND, `L` L, `LL` LL, `LU` LU, `MS` MS, `N` N, `NCL` NCL, `ND` ND, `NEG` NEG, `NR` NR, `NS` NS, `POS` POS, `R` R, `RR` RR, `S` S, `SDD` SDD, `SYN-R` SYN-R, `SYN-S` SYN-S, `U` U, `UNE` UNE, `VS` VS, `W` W, `WR` WR

### Table 0119, order control, to ServiceRequest.status

IG map: none

`NW` active, `CA` revoked

### Table 0125, value type, to Observation.value[x]

IG map: [ConceptMap-segment-obx-to-observation](https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-obx-to-observation.html)

`NM` valueQuantity, `ST` valueString, `TX` valueString, `FT` valueString, `CE` valueCodeableConcept, `CWE` valueCodeableConcept, `DT` valueDateTime, `DTM` valueDateTime, `TS` valueDateTime

### Table 0396, coding system, to Coding.system

IG map: none

`LN` http://loinc.org, `SCT` http://snomed.info/sct, `UCUM` http://unitsofmeasure.org
