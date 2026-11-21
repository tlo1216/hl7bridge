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
- Patients merged away (A40): see the Update below. Rows whose subject
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

## Update, ADT^A40 merge

`to-fhir` and `fhir sync` map ADT^A40. The survivor (PID) is merged as for any
ADT. The merged-away Patient (MRG-1) is set `active=false` with a
`replaced-by` link to the survivor. The survivor gets no link, so a second
merge into it rewrites nothing.

- The v2-to-FHIR IG 1.0.0 has no ADT_A40 message map and no MRG-to-Patient
  map (its only MRG map targets Account, with MRG-1 unmapped), so this is
  hl7bridge's own reading of `Patient.link`, and the spec row says so.
- `active` and `link` are owned elements in the post merge. `link` is
  additive: entries on the server stay, the message's is added when no entry of
  the same type and target is there.
- Refused before anything is written: more than one PID or MRG, a repeated,
  empty or null MRG-1, MRG-1.5 other than MR, MRG-1.4 different from PID-3.4,
  an MRG-1 equal to PID-3, a merged-away Patient already replaced by another
  Patient, a survivor that is inactive or merged away itself (a reversed merge),
  and two entries that resolve to one resource on the server. A resend of a
  merge already on the server passes, even when its survivor has since been
  merged away itself (A to B, B to C, then A to B again): the resend writes B's
  demographics onto B, as any later message to a merged-away Patient does.
- Chains are posted edge by edge (A to B, then B to C); they are not flattened.
- Not done: orders, reports and encounters of the merged-away Patient are not
  moved onto the survivor, and nothing reactivates a merged-away Patient, so
  later messages can still write onto it.
- The offline Bundle makes the merged-away Patient a conditional create, never
  a PUT, so a file posted as is cannot replace a Patient with a bare body. Its
  survivor entry is still a conditional PUT of the message's own Patient, so
  posting the file replaces the survivor with that content (a PID without an
  address erases the server's); only `--post` merges.
- `fhir sync` posts every stored message in order, the merged-away patient's
  own messages too, so it and `to-fhir --post` write the same content.
- The merged-away entry is a conditional create when the server has none. If
  another writer creates it between the read and the commit, the survivor
  update commits and the post raises "send again"; a resend is safe.
- A bare stub has no `meta.profile` and no required US Core elements, so a
  server that enforces US Core may refuse it.
