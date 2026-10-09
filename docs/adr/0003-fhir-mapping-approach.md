# ADR-0003: FHIR mapping approach (R4B models, conditional update)

Date: 2026-10-09
Status: accepted

## Context

Project 1 posts Patient, ServiceRequest, DiagnosticReport, and ImagingStudy
to a HAPI FHIR server (`hapiproject/hapi`, default FHIR version R4; CI pins
image `v8.12.0-2`). `fhir.resources` 8.x dropped the plain-R4 package and
ships R4B; for these four resource types R4B is structurally identical to
R4, so R4B-validated JSON is what we send to the R4 server. The CI system
test against real HAPI is the check on that claim.

## Decisions

1. **Models**: `fhir.resources.R4B` (pydantic v2). Every resource is
   validated locally before it is sent; mapping failures raise
   `FhirMappingError` and unknown report statuses are refused, never
   guessed (quarantined upstream at ingest as well).
2. **Idempotency**: conditional update, `PUT <type>?identifier=system|value`.
   Zero matches creates, one match updates in place, multiple matches is a
   412 we surface. Re-running the sync creates nothing new (asserted by the
   HAPI system test, including for corrected reports).
3. **Identifiers**: synthetic URN systems per hospital
   (`urn:hl7bridge:<hospital>:mrn`, `...:accession` with v2-0203 type ACSN)
   and `urn:dicom:uid` / `urn:oid:<StudyInstanceUID>` for studies. The
   accession identifier is the thread joining ServiceRequest,
   DiagnosticReport, and ImagingStudy, mirroring the HL7/DICOM bridge.
4. **References**: sequential upserts in dependency order (Patient, then
   ServiceRequest, then DiagnosticReport / ImagingStudy), using the server
   ids returned by each upsert. We chose this over a transaction Bundle
   with conditional references because each failure then has one URL, one
   payload, and one server response to look at.
5. **Status maps**: OBR-25 `F/P/C` -> `final/preliminary/corrected`;
   order status `new/cancelled` -> ServiceRequest `active/revoked`.

## Deviations from the HL7 v2-to-FHIR IG

The IG is followed directionally (PID->Patient, ORC/OBR->ServiceRequest,
OBR/OBX->DiagnosticReport, ACSN identifier type), with these conscious
simplifications, acceptable for a synthetic-data demo and flagged for any
real deployment:

- Identifier systems are synthetic URNs, not site-assigned OIDs.
- OBX segments collapse into `DiagnosticReport.conclusion` instead of
  Observation resources (`presentedForm` would be the next step).
- No Provenance, Encounter, or practitioner resources.
- Patients merged away (A40) are simply not posted; nothing resolves merge
  chains onto resources yet (that is project 8's scope). Rows whose subject
  cannot be resolved are skipped loudly and reported, never dropped.
