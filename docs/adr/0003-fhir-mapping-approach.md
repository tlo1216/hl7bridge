# ADR-0003: FHIR mapping approach (R4B models, conditional update)

Date: 2026-10-09
Status: accepted

## Context

The FHIR bridge posts Patient, ServiceRequest, DiagnosticReport, and ImagingStudy
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
- Patients merged away (A40): `to-fhir` and `fhir sync` set the merged-away
  Patient inactive with a `replaced-by` link to the survivor. Nothing moves
  earlier resources onto the survivor and chains are not followed; `fhir sync`
  still leaves out the merged-away patient's own messages. Rows whose subject
  cannot be resolved are skipped loudly and reported, never dropped.

## Update, 0.4.0

- The message path (`hl7bridge.v2fhir`, used by `to-fhir`) maps every
  structured OBX to an Observation and posts one transaction Bundle per
  message, with `urn:uuid` references. Narrative OBX (TX, FT) is the
  report text in `conclusion` and `presentedForm`. A transaction fits
  there because one message is one unit: either all of it is committed or
  none of it is, and `to-fhir --post` checks the answer entry by entry.
- Only ADT writes the Patient; ORM and ORU create it when missing
  (`ifNoneExist`). Patient from ADT, Encounter and ServiceRequest are
  merged into the server's current version before they are written (an
  empty v2 field means "no change"), with `If-Match` on the version read.
  A correction sets each Observation it no longer carries to
  `entered-in-error`. All of this is in `fhir/post.py`.
- `fhir sync` rebuilds Patient, Encounter, ServiceRequest, Observation and
  DiagnosticReport from the stored messages through the same mapping and
  the same post, one transaction per message, so the two paths write
  identical content and never undo each other. Only ImagingStudy is still
  built from store rows, as an upsert (decision 4).
- Both paths share the code tables (sex, patient class, report status,
  order status) and the identifier keys, so they update the same
  resources. A key value holding a FHIR token special character (`,` `|`
  `\` `$`) or a control character is refused, never escaped. Status maps follow the IG's vocabulary maps; a code outside a
  table is refused. A sex code outside table 0001 used to become
  `unknown` on the store path and is now refused, with the row skipped
  and reported.
- Identifier systems come from the hospital profile
  (`fhir.identifier_systems`); the URNs above stay the defaults.
- DiagnosticReport now carries `presentedForm` (the report text as
  plain text) beside `conclusion`, takes its code from its own OBR-4, its
  effective time from OBR-7 and its issued time from OBR-22, as the IG
  does. Before, `issued` was the time hl7bridge received the message.
- Encounter is mapped from PV1 when PV1-19 holds a visit number, keyed on
  it. Provenance and practitioner resources are still not mapped.
- Every field each mapper reads, and every deviation from the IG, is a row
  in `src/hl7bridge/v2fhir/spec.py`; `docs/mapping.md` is generated from it.
