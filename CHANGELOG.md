# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/). Until 1.0.0 the CLI and the
profile format may still change between minor versions.

## [Unreleased]

### Fixed

- Last review of MLLP over TLS. Shutdown of `listen --target fhir` no longer
  closes the FHIR client and the direct-mode log under a worker that is still
  committing: the worker finishes and writes its log row, and the process
  exits after it. `replay` again sends quarantined rows with no control id
  (and unparseable ones) and reports the listener's AR; only `send` refuses a
  message no ACK can name. Control ids are read once, the listener's way, on
  both sides: leading whitespace skipped, only separator and hex escapes
  decoded (any other escape, or an MSH that is not UTF-8, names no control id),
  control characters dropped, then compared; `send` also refuses an MSH-10
  that is `UNKNOWN` or empty after that reading, and two different messages
  whose MSH-10 reads the same. An ACK now needs exactly one MSH, and MSA-1 must
  be exactly one ACK code (the whole field) before MSA-2 is compared; an
  `UNKNOWN` MSA-2 counts only in a negative ACK, whatever was sent. Reading an ACK's fields can no longer expand
  an escape past the frame or raise. `send_messages` raises ValueError when it
  reaches a payload with no usable control id, before writing it, and sends
  nothing more, unless `allow_missing_control_id=True`; it still reads its
  input one message at a time. An MSH whose encoding characters repeat a
  separator is a parse error, not an AssertionError. New `listen` flags
  `--max-connections-per-address` (default 32) and `--max-pending-per-address`
  (default 4); `serve` refuses a connection limit below 1. A TLS upgrade that
  ends with no transport is a failed handshake on both sides, not an
  AttributeError. A second SIGTERM is ignored while the event loop runs;
  after it returns, one can stop the wait for the FHIR workers. The
  client-certificate issuer is logged at DEBUG only, like the subject. A
  sender whose first address family is unavailable tries the next lookup
  result. `docs/threat-model.md` has a Known limits section.

- Final review of MLLP over TLS. An ACK counts only when the reply starts with
  MSH, has MSH-9 `ACK` and exactly one MSA, and its MSA-2 is the MSH-10 sent,
  both read the same way (trimmed, first component, escapes decoded); `UNKNOWN`
  counts only in a negative ACK. `send` and `replay` refuse a message with an
  empty MSH-10 before sending (exit 2); `send_messages` still sends one but
  accepts only a negative `UNKNOWN` ACK for it, never an AA.
  The listener echoes a readable MSH-10 in its AR for an unparseable message or
  an internal error, so `replay` of such rows and a non-UTF-8 `send` report the
  AR instead of a protocol error. TLS listeners cap open connections
  (`--max-connections`, default 256) and pending handshakes from one address
  (4). SIGTERM on Linux and macOS takes the Ctrl+C path, so a busy client gets
  its grace. Shutdown cancels a client still in its handshake instead of
  aborting it, and the direct listener waits at most 5 seconds for its FHIR
  workers. A maximum-size frame whose terminator splits across two reads is a
  frame. A stuck name lookup no longer holds `send` past `--timeout`. Failed
  handshake lines are capped at 20 a minute across all peers. A handshake reset
  is no longer reported as a stall. `--show-ack-text` follows the ACK's sign,
  not `--expect`. The docs no longer suggest `--tls-ca` works around the
  Windows Disallowed store.

- Third review of MLLP over TLS. An empty `--host` is a usage error for `send`,
  `replay` and `listen` (and `send_messages` and `serve` raise `ValueError`):
  it could skip the host name check or bind every interface. A TLS flag value
  is never echoed, only the flag name. An ACK counts only when its MSA-2 equals
  the MSH-10 sent; a truncated, non-ACK or stale reply is `AckProtocolError`
  (exit 3), keeping the ACKs already confirmed. Behaviour change: by default
  `send` and `replay` no longer print the receiver's MSA-3 or ERR text for an
  AE or AR (it can quote patient data); add `--show-ack-text` to see it. The
  quarantine reason line for hl7bridge's own reason codes stays. The failed
  handshake table is a fixed-size LRU (1024 peers) and at most 64 handshakes
  may be pending. Shutdown (`shutdown_server`) closes idle clients at once and
  cuts a busy one after 5 seconds. `send_messages` refuses a TLS context that
  does not check the host name or require a certificate. Host names match
  subject alternative names only; partial-chain verification is on only with
  `--tls-ca`. The client certificate audit line carries the SHA-256
  fingerprint and percent-encoded fields. The `sent` total counts frames written. `--timeout` is one
  deadline for connecting (lookup, TCP, handshake) and one for each write plus
  ACK, and the reported value is the real one. The 1,000,000 byte frame cap
  is exact. `replay` checks TLS options before reading the store, also for a
  dry run or an empty selection. Docs state the Windows Disallowed-store gap and
  that Windows process termination skips the clean shutdown.

- Review fixes for the repeating PID fields. Two identifiers with no system
  and the same value, from different assigning authorities, are both kept
  (in the mapping and in the merge with the server's copy). An explicit HL7
  null (two double quotes, v2.8.2 section 2.10.5) in PID-5, PID-11, PID-13 or
  PID-14 now clears the name, address or telephone list on the server; a field
  left out still keeps it. A telephone repetition is mapped by the IG's XTN
  rules (XTN.12, else XTN.1, else a number built from XTN.5 to XTN.7) and a
  repetition with a value is never dropped. The PID-13 and PID-14 default use
  applies only when XTN.2 is empty; a code the IG leaves unmapped (EMR, NET)
  gives no use. The `Committed.key` of a retired Observation reads the
  report's own identifier, not the first one the server lists. In
  `docs/conformance.md` the telecom assertion now compares system, value and
  use, and the DiagnosticReport identifier assertion expects both FILL and
  PLAC; the counts are unchanged (GAP 37, GAP rejected 3, LOCAL 5, MATCH 81).

### Added

- Optional MLLP over TLS: `listen --tls-cert --tls-key [--tls-ca]` (client
  certificates with `--tls-ca`), and `send` and `replay` `--tls`, `--tls-ca`,
  `--tls-cert`, `--tls-key`. TLS 1.2 minimum, certificate and host name always
  verified. New `docs/threat-model.md`. Empty TLS flag values are a usage
  error. The listener logs and counts failed handshakes, logs each accepted
  client certificate, and warns when TLS has no `--tls-ca` on a non-loopback
  bind. Encrypted keys are refused with a message. `send` reports "broke
  after N of M ACKs" instead of "Nothing was sent" once a frame was written.
  A failed connect (refused, no route, unknown host, timed out), with or
  without TLS, is one "Never connected, nothing was sent" error; after a
  break only the first message without an ACK is "may or may not have been
  processed", the rest are "not sent". The accepted client certificate line
  (peer, SHA-256 fingerprint, serial) is always written, sanitized,
  at a level `--quiet` keeps; the subject, alternative names and issuer are
  logged only at DEBUG. Failed-handshake warnings are limited to one a
  minute per peer with a summary, include the check text, and the total is
  logged at any shutdown (SIGTERM stops the listener like Ctrl+C). `--timeout`
  must be a finite number above 0. Bytes of a half-read frame are quarantined
  when a connection breaks. `TlsHandshakeError` moved from
  `hl7bridge.ingest.mllp` to `hl7bridge.errors` and is now a `BridgeError`
  (a `NetworkError`); the new `ConnectError` is its sibling for a connect that
  failed. `send_messages` raises `ConnectError` instead of the raw `OSError`
  when it cannot connect.

- `to-fhir` and `fhir sync` keep every repeat of PID-3 (identifiers, each with
  its type from PID-3.5 and its assigning authority from PID-3.4), PID-5
  (names, with prefix, suffix and use), PID-11 (addresses, with use and
  district) and PID-13 and PID-14 (telephone numbers and email addresses),
  following the IG 1.0.0 CX, XPN, XAD and XTN maps, where only the first was
  written before. The MRN repetition is still written first and is still the
  key: conditional URLs, full URLs and `Committed.key` are unchanged. In
  `docs/conformance.md` the PID-3, PID-5, PID-11 and PID-13 repeat assertions
  move from gap to match (the PID-3.4 system assertion stays a gap: the MRN
  system comes from the profile).

- Reproducible IG 1.0.0 conformance evidence in `docs/conformance.md`, with
  synthetic ADT A01/A04/A08/A40, ORM O01 and ORU R01 fixtures, a separate
  structured-number rejection probe, a CLI comparison tool and regression tests.

- `to-fhir` and `fhir sync` map ADT^A40 (patient merge). The surviving
  Patient (PID) is merged as for any ADT; the merged-away Patient (MRG-1) is
  set inactive with a `replaced-by` link to the survivor, keeping what the
  server already holds for it (its other links stay), or written bare when
  the server has none. The offline Bundle makes the merged-away Patient a
  conditional create, so a file posted as is never replaces it with a bare
  body. The offline file does not apply the merge (the survivor entry is
  still a conditional update and an existing merged-away Patient is left
  untouched); use `--post` to apply one.
  Refused: a message with more than one PID or MRG, an MRG-1 that is empty,
  repeated, typed with anything but MR, the same as PID-3 or from another
  assigning authority than PID-3, an A40 whose merged-away Patient is
  already replaced by another Patient, one whose survivor is inactive or
  merged away itself, and one whose two Patients are one resource on the
  server. `fhir sync` posts every A40 in order, chains edge by edge, and
  the merged-away patient's own messages too, as `to-fhir --post` does; an
  A40 the server contradicts is skipped loudly. Orders, reports and
  encounters of the merged-away Patient are not moved onto the survivor, and
  nothing reactivates a merged-away Patient: later messages can still
  write onto it. A bare merged-away Patient may fail a server that enforces
  US Core. `listen --target fhir` still answers A40 with AR
  `patient_read_only`.

### Fixed

- `--post` and `fhir sync` of an ADT^A40 write the resolved `Patient/<id>` in
  the `replaced-by` link when both Patients exist, not a `urn:uuid`.
- The key reported for a merged Patient is its MRN, not the server's first
  identifier.

### Changed

- `merge_resource` keeps identifiers apart by type when they have no system.
  A post still replaces a Patient's names, addresses and telephone numbers
  whole with the message's repeats (each ADT carries the sender's complete
  current list, so a corrected address does not sit beside the old one) and
  still adds to the identifiers and links the server holds.

- `fhir sync` no longer leaves a merged-away patient out. Its messages and
  its ImagingStudy rows are posted like any other patient's, so the sync
  and `to-fhir --post` agree; the "messages left out (patient merged into
  another)" line of the report and `FhirSyncReport.merged_away` are gone.
- `to-fhir --post` and `fhir sync` fail an ADT^A40 the server contradicts,
  and a Patient another writer created mid-merge, with exit 1 (findings),
  not exit 3 (connection error). A lost create on either side of a merge
  says the merge may be only partly applied and to send again.

## [0.5.0] - 2026-11-20

### Added

- `listen --target fhir`: the direct-to-FHIR listener. Each message is one
  FHIR transaction, and the ACK goes back after the commit: AA only for a
  validated `transaction-response`, AE for anything a resend or a fix can
  cure (unreachable server, timeout, lost answer, mapping error, patient
  matching, run ownership, stale result), AR for what it cannot (a type
  the listener does not take, MSH-11 outside `allowed_processing_ids`,
  unparseable input). The table is in `ingest/direct.py`; the reasons in
  ADR-0004. The FHIR calls run in a worker thread with a per-request
  timeout (`--fhir-timeout`).
- Patient matching by MRN (`fhir/match.py`): the count is over the whole
  result (`_total=accurate`, `Bundle.total`, a `next` link), and none,
  several, an inactive Patient or one replaced by another is AE. The
  Patient is never written in direct mode; A08 and A40 get AR
  `patient_read_only`.
- Run ownership: `--run-id` (or `HL7BRIDGE_RUN_ID`) is required; every
  identifier written is namespaced by the run, every resource carries the
  lab tag and the run tag, and a key that matches a resource outside the
  run is AE `outside_run`.
- One writer per key: per-identity and per-key locks while a transaction
  is in flight, so the same message on two connections leaves one set.
- Message identity for direct mode: hospital, MSH-3, MSH-4 and MSH-10 in
  the run, plus a hash of the whole message. The same bytes are a
  duplicate (AA, nothing written); any change, MSH-7 included, is AE
  `control_id_conflict`. `resend_ignores` is not a profile key.
- `fhir reset --run-id NAME`: deletes one run's Observations,
  DiagnosticReports, ServiceRequests and Encounters in one transaction,
  after a dry run and the run id typed back. Never a Patient, never more
  than one run.
- `from-fhir --patient ID` writes sample A04, A08, ORM and ORU messages
  for a Patient on a FHIR server (`--report ID`: an ORU for a
  DiagnosticReport and its Observations).
- `--mapping-module FILE` on `listen --target fhir` and `to-fhir`: mapping
  hooks for `sex_code`, `abnormal_flag` and `observation_time`, with their
  results checked. For loopback teaching labs only; the command warns.
- Profile keys `fhir.patient_policy`, `fhir.tag`,
  `fhir.allowed_processing_ids` and
  `fhir.identifier_systems.mrn_by_authority`, and an example lab profile
  in `profiles/examples/wintehr.yaml` (not loaded by default).

### Changed

- The synthetic generator writes MSH-11 `T` (training) instead of `P`.
- Direct mode hardening before release: ACKs escape every echoed header
  value (the store listener too); a valued PID-3.4 the profile does not
  map is AE `unknown_assigning_authority`; a key of another Patient is AE
  `patient_mismatch`; the secondary identifier moves to
  `<system>/as-sent`; a maybe-committed message holds its control id; a
  logged duplicate is answered AA only while its keys are still in the
  run, and log rows are bound to the FHIR base (schema version 2, AA rows
  keep no bytes, the file is held exclusively); a per-message deadline
  (AE `deadline_exceeded`), bounded lock waits and an own worker pool; a
  log write failure is AE `log_write_failed` and an unreadable server
  answer AE `fhir_unconfirmed`, never AR.
- `fhir reset` deletes conditionally on the run tag, checks every id,
  takes only 200 or 204, and voids the run's rows in the listener's log
  with `--db`. A search answer holding another resource type, or a paged
  identifier search with an empty first page, is an error.
- `to-fhir --post` and `fhir sync` refuse a `match_only` profile.
- A retraction (cancelled, entered-in-error) ranks above a correction in
  the stale-report rule.
- `from-fhir` takes the MRN only from the profile's MRN system or a mapped
  one, refuses a malformed `birthDate`, drops control characters and adds
  a patient hash to MSH-10 and the sample keys.
- Mapping hooks: a misbehaving hook (an unhashable answer, SystemExit) is
  a mapping error; a hook name bound to a non-function is refused;
  `--mapping-module` is refused off loopback.
- `to-fhir --post` and `fhir sync` refuse a report older than the one on
  the server (OBR-22, then status); sync lists it as skipped.
- A merged resource (Patient from ADT, Encounter, ServiceRequest) that a
  search did not find is now made by a conditional create that must answer
  201. If another writer created it in between, the post fails with "send
  again" instead of overwriting it unmerged.

### Fixed

- A correction retires only results whose key parses to the report's own
  accession, and a narrative ORU whose accession holds `/` is refused like
  a structured one, so no report can retire another report's results.
- `fhir sync` replays the stored ADT, ORM and ORU messages in the order
  they arrived, across types. An ORU that a later version replaced posts
  its Patient entry only. Sync and a direct post now keep the same Patient
  demographics when no ADT came first.

## [0.4.0] - Unreleased

### Breaking changes

- The command names renamed in 0.3.0 are gone: `serve`, `fhir-sync`,
  `fhir-probe`, `load-studies` and `load-outbound` now exit 2 as unknown
  commands. Use `listen`, `fhir sync`, `fhir probe`, `load studies` and
  `load outbound`. `scripts/demo.sh` is removed; use `hl7bridge demo`.
- The database schema is version 3. New: the `encounters` table (PV1 of
  A01, A04 and A08 by PV1-19, with `last_message_id`); on `reports`, the
  report's own OBR-4 (`procedure_code`, `procedure_description`,
  `code_system`), its OBR-7 time (`observed_at_utc`) and `message_id`, the
  `messages` row it came from; on `report_events`, `message_id`. The
  `messages` and `quarantine` tables did not change. There is still no
  migration: a version 2 database gives the "old schema" message for every
  command except `replay`, which reads it so its messages can be resent
  into a new store. Demo databases can be made again.
- `fhir sync` now rebuilds Patient, Encounter, ServiceRequest, Observation
  and DiagnosticReport from the stored messages (`messages.raw`) through
  the same mapping and post as `to-fhir --post`: every accepted ADT A01,
  A04 and A08 and every ORM in received order, then for each report the
  ORU it came from (`reports.message_id`). The two paths write identical
  resources, so neither undoes the other. What this changes for sync:
  - DiagnosticReport gains `result` (one Observation per structured OBX),
    and `basedOn` is a logical reference by the accession identifier
    (type ServiceRequest, ACSN) instead of `ServiceRequest/<id>`.
  - Patient gains address and telecom from PID-11 and PID-13, and is
    merged, never overwritten (see below). ServiceRequest from a cancel
    keeps the NW's `authoredOn`.
  - A message that cannot be mapped is skipped and listed by message type
    and store row (`ADT^A08 HOSPC/message 12: PID-8: ...`), instead of a
    patient or visit row. A message for a patient merged by A40 is left
    out and counted, as merged patients were before.
  - The report counts Observations, and the Observations a correction
    retired.
  - ImagingStudy is still built from the `studies` rows.
  The row mappers in `hl7bridge.fhir.mapper` stay for library callers;
  `fhir sync` uses only `imaging_study_resource` now.
- Only ADT writes the Patient. In ORM and ORU Bundles the Patient entry is
  a conditional create (`POST` with `ifNoneExist`): created when missing,
  never changed, so a thin PID in a result no longer erases the address
  and phone an ADT wrote. Patient from ADT, Encounter and ServiceRequest
  are merged into the server's current version when posted (read by
  identifier, the message's elements laid over it, written to
  `<Type>/<id>` with `If-Match`): an empty v2 field means "no change", so
  an A08 without PV1-44 keeps the admit time, and a discharge stays
  finished. A change between the read and the write fails the transaction
  (HAPI answers 409); more than one resource with the key is refused.
- A corrected report that no longer carries a result sets that
  Observation to `entered-in-error` when posted, in the same transaction;
  Observations of another report or another system are never touched.
  Results posted by a development build of 0.4.0 under the older key form
  (`<accession>/<code>/<sub-id>`, `set-<n>`) are retired by the next
  post of their report.
- Every identifier used in a conditional URL (PID-3, PV1-19, the
  accession, each result key part) is refused when it holds `,` `|` `\`
  `$` or a control character, after HL7 unescaping. A comma made the
  search an OR that could update another patient.
- PID-3: when any repetition carries an identifier type (PID-3.5), the
  first one typed MR is the MRN, and a typed list without MR is refused
  (AE `missing_mrn` at ingest). Untyped PID-3 still uses the first
  repetition. The store and the FHIR mapping use the same rule.
- A time with no offset is read in the MSH-7 offset when MSH-7 has one,
  then in the hospital's zone, as HL7 v2 defines it. This applies at
  ingest (stored order and report times) and in the FHIR mapping. A date
  with no time is written as a FHIR date where the element allows it, and
  refused for `DiagnosticReport.issued` (an instant) instead of padded to
  midnight.
- `to-fhir` decodes input as strict UTF-8: a file that is not UTF-8 is an
  error, not U+FFFD. `explain` still shows such bytes as U+FFFD.
- `to-fhir --post` posts nothing when any message fails to map, since a
  later message can depend on it; `--skip-failures` posts the rest. It
  also refuses a run that would post an older version of a report (by
  OBR-22, then OBR-25) after a newer one.
- An ORM with more than one order (ORC and OBR pair) is refused by the
  FHIR mapping instead of mapping the first.
- `send` sends each message of a file or stdin in its own MLLP frame (a
  new message starts at each MSH), and refuses input that does not start
  with MSH. It used to put a batch file in one frame.
- `replay` sends each distinct stored message once (the oldest copy) and
  says how many duplicates it skipped.
- `send --expect CODE` lists each ACK whose code differs from CODE
  (it listed every one that was not AA).
- `fhir sync` refuses codes it cannot map instead of defaulting them. A
  patient whose sex code is not in table 0001 used to be posted with
  gender `unknown`; it is now skipped and listed with the field and the
  reason, and its rows are skipped as having no subject.
- DiagnosticReport from `fhir sync` follows the IG for its times and code:
  `effectiveDateTime` is OBR-7 (was OBR-22), `issued` is OBR-22 (was the
  time hl7bridge received the message), and the code is the ORU's own
  OBR-4 (was the order's). It also gains a plain-text `presentedForm`.
  ServiceRequest gains the placer order number (ORC-2, else OBR-2) as a
  second identifier with type PLAC; the store fills it from OBR-2 when
  ORC-2 is empty.

### Added

- `hl7bridge explain`: a message one field per line, with field names for
  the common segments, components split out, repetitions numbered, coded
  fields decoded (`OBR-25 Result status: F = final`) and empty fields
  hidden unless `--all`. Reads a file, a folder or stdin; `--json`.
- `hl7bridge to-fhir`: each message becomes a FHIR transaction Bundle of
  conditional updates, printed as JSON with no server involved, written
  one file per message with `--out DIR` (never over an existing file), or
  posted in order with `--post`. A post counts as committed only when the
  answer is a `transaction-response` with one entry per request and every
  entry at 200 or 201; the first Bundle that falls short stops the run.
- `hl7bridge.v2fhir`: pure functions from a parsed message to FHIR R4B
  resources. PID to Patient (identifier, name, birth date, sex, address,
  telecom), PV1 to Encounter for ADT A01, A04 and A08 (one mapping; an A08
  updates the same Encounter), ORC and OBR to ServiceRequest, OBR to
  DiagnosticReport, and every structured OBX to an Observation linked
  from `DiagnosticReport.result`: NM to `valueQuantity` (UCUM only when
  OBX-6 says so; the digits kept as sent), ST to `valueString`, CE and CWE
  to `valueCodeableConcept`, DT, DTM and TS to `valueDateTime`, OBX-7 to
  the reference range text, OBX-8 to interpretation, OBX-11 to status,
  OBX-14 to the effective time, and the NTE after an OBX to its note.
  Narrative OBX (TX, FT) is the report text in `conclusion` and
  `presentedForm`, not an Observation per line. Text values and NTE-3 are
  read a whole repetition at a time, so a stray `^` does not cut a line.
  Each result is keyed `<accession>/<OBX-3.1>/sub/<OBX-4>`, or
  `.../set/<OBX-1>` when OBX-4 is empty, so a resend updates in place and
  an explicit sub-id never collides with a set id. Codes go through tables that
  follow the HL7 v2-to-FHIR IG's vocabulary maps; a code outside a table,
  an OBX-2 hl7bridge does not map, or an NM value that is not a plain
  number is refused with the field position and never guessed.
- The mapping spec (`src/hl7bridge/v2fhir/spec.py`): every field each
  mapper reads, the FHIR element it writes, the IG page it follows and
  where it differs. `docs/mapping.md` is generated from it
  (`tools/gen_mapping_doc.py`), and tests fail when the two differ, when a
  mapper reads a field with no row, or when a row is never read.
- Visits: the store keeps PV1 from ADT A01, A04 and A08 when PV1-19 holds
  a visit number, and `fhir sync` posts them as Encounters.
- `hl7bridge replay`: resend quarantined messages (or, with `--from
  store`, stored ones) oldest first and byte for byte, with
  `--only-reason`, `--since`, `--hospital`, `--limit` and `--dry-run`. It
  opens the store read-only and leaves out bytes received outside MLLP
  framing.
- `send` reads stdin (`-`) and takes `--repeat N` (the same bytes again,
  never restamped), `--rate` (messages per second) and `--expect CODE`
  (exit 1 when any ACK differs). `send` and `replay` warn when the host
  is not loopback.
- Profile block `fhir.identifier_systems` (`mrn`, `accession`, `placer`,
  `visit`, `observation`), validated strictly; the defaults are the
  `urn:hl7bridge:<hospital>:...` systems used before.
- Three samples: an inpatient A01 with a visit number, its A08 discharge,
  and an ORU with numeric, coded and text results and a note. HOSPC lists
  ADT^A01.
- `MappingError`, whose text is built only from a field position and a
  fixed reason; it has no argument for a value. A field with a malformed
  escape sequence, a value the FHIR model refuses, and a timestamp out of
  range are mapping errors too, with fixed reasons.
- `hl7bridge.fhir.post`: the post `to-fhir --post` and `fhir sync` share
  (merge, retire, the run-order check). `HapiClient.commit`,
  `search_identifier` and `read_by_id`.
- `hl7bridge.v2fhir.jsontext`: one JSON writer for files and posts that
  keeps each decimal's digits (`12.50` stays `12.50`; `fhir.resources`
  writes floats).
- CI checks `docs/mapping.md` against the spec, runs `explain` and
  `to-fhir` from the installed wheel, and runs the new `to-fhir` HAPI
  system test beside the sync one.

### Changed

- The demo's threat model is written down in `demofiles.py`, the README
  and `docs/cli.md`: what deletion guarantees, and that the races left
  need write access to the user's own private temp folder. A test checks
  that the folder comes from `mkdtemp` and is mode 0700 on POSIX.
- `send_messages` takes `interval_s`, the pause between an ACK and the
  next send.

### Fixed

- python-hl7 logs a malformed escape sequence or a stray segment with the
  whole field or segment text and a traceback. Its records are now
  replaced by a fixed line on its own loggers and in the CLI handler, and
  the FHIR mapping refuses such a field first.
- Identifier searches and counts send `Cache-Control: no-cache`: HAPI
  reuses a recent search result by default, so a merge could read an
  answer from before the resource existed.
- `fhir sync` skips an ImagingStudy whose identifier cannot be a search
  key instead of stopping.
- `replay --dry-run` strips control characters from stored fields, and a
  bad `--since` names `--since` in the error (it said `--now`).

### Security

- SECURITY PATCH: certifi 2026.7.22 to 2026.10.23 in `uv.lock`, for
  GHSA-6q48-6c7w-9gw2 (PYSEC-2026-4320, emSign root CAs with compromised
  intermediate keys; fixed in 2026.10.18). certifi is a runtime dependency
  through httpx. The build lock (`.github/requirements/build.txt`) holds
  no certifi.

### Not changed, on purpose

- The pip install CI job still resolves the wheel's runtime dependencies
  unhashed, the way a pip user gets them; see `docs/testing.md`.
- Ingest identity stays hospital and MSH-10. Replay and `--repeat` send
  stored bytes unchanged, so they stay correct when identity widens to
  MSH-3 and MSH-4 with a body hash; the new `message_id` columns are the
  reference later features join on.

## [0.3.0] - Unreleased

### Breaking changes

- Commands are renamed and grouped: `serve` is `listen`, `fhir-sync` is
  `fhir sync`, `fhir-probe` is `fhir probe`, `load-studies` is
  `load studies`, `load-outbound` is `load outbound`. The old names still
  work in 0.3.x with a warning and go away in 0.4.0.
- `lint` and `send` take the file or folder as an argument
  (`hl7bridge lint msgs`). `--dir` is still accepted.
- The profiles moved from `config/hospitals/` to `src/hl7bridge/profiles/`
  and ship inside the wheel. `--config` is no longer required: the lookup
  is `--config`, `HL7BRIDGE_CONFIG`, `./hl7bridge/profiles`, then the
  bundled profiles. Scripts that passed `--config config/hospitals` must
  drop the option or point at the new folder.
- `--db` is no longer required: `--db`, `HL7BRIDGE_DB`, then
  `./hl7bridge.db`. `listen` and `load` create the database; `summary`,
  `reconcile`, `ack-report`, `tat` and `fhir sync` open an existing one
  only and exit 2 if there is none, so a report run in the wrong folder no
  longer leaves an empty database behind. A SQLite file holding another
  program's tables is refused instead of getting hl7bridge's tables added.
- `fhir sync` needs a server named with `--fhir-base` or
  `HL7BRIDGE_FHIR_BASE`; it no longer falls back to
  `http://localhost:8080/fhir`. `fhir probe` and `doctor` still do, and send
  no credentials to that fallback.
- A `--fhir-base` or `HL7BRIDGE_FHIR_BASE` that is not an http or https URL
  with a host and no query exits 2, without echoing the value.
- Exit codes are fixed and documented in `docs/cli.md`: 0 success, 1
  findings or rejected messages, 2 usage or configuration error, 3
  connection error, 4 internal error, 130 Ctrl+C. Before, a configuration
  error exited 1 and an internal error 2.
- `send` exits 1 when any message gets AE or AR, and 3 when no ACK comes
  in time or the listener does not answer. It exited 0 before.
- `fhir sync` and `fhir probe` exit 3 when the server never answered.
- Error lines start with `error:` instead of `hl7bridge: error:`, and end
  with a hint. Errors whose text could quote input no longer print stack
  frames unless `--debug` is given.
- Log output is quieter by default: WARNING for every command except
  `listen`, which logs at INFO. `-v` and `-vv` bring INFO and DEBUG back.
- A database with another schema version, or a file that is not a
  database, raises a clear error instead of a `RuntimeError`.
- `scripts/demo.sh` is deprecated and goes away in 0.4.0. It now only runs
  `hl7bridge demo` with the arguments given: no HAPI container, and no
  deleting. `scripts/demo_guard.sh` is removed. Use the compose demo for
  HAPI in containers.
- `demo --data-dir` takes only a new or empty folder, writes there and
  never deletes anything in it. Reusing an earlier demo folder, and the
  `.hl7bridge-demo` marker that allowed it, are gone.
- `tat --month` must be a real month (`YYYY-MM`, 01 to 12), and missing
  input files (`load outbound --path`, `load studies --dicom`), an output
  path that is a file (`synth --out`, `init`), and file system errors
  exit 2 with a message, not 4 ("a bug in hl7bridge").
- The demo exits 1 when the FHIR step skipped a row, and an HTTP error
  from the FHIR server's `/metadata` exits 1 (3 is only for no answer).

### Added

- `hl7bridge demo`: the whole pipeline in one process on every OS, with a
  listener on a free loopback port, a check of every count against the
  manifest, and an optional FHIR step against a server named with
  `--fhir-base` (`--fhir-wait` polls a server that is still starting). By
  default the demo writes into a new temporary folder and removes it after
  a clean run, unless `--keep`. It removes only what it created in that
  run: each file and folder is created exclusively and recorded with its
  file identity, and is deleted only while it still has that identity,
  never through a link (on Windows through the handle that was checked). Anything else is left and the folder is kept.
  `hl7bridge.demofiles` is the only code in hl7bridge that deletes.
- `hl7bridge init [FOLDER]`: copies the bundled profiles and twelve
  hand-made sample messages (one per type and dialect, two broken). Never
  overwrites without `--force`.
- `hl7bridge doctor`: Python, version, profiles, time zones, database
  (read-only), listen port and FHIR server, each with a fix.
- `hl7bridge profile show` and `profile check`, and `docs/profiles.md`
  with every key, its type, default and purpose.
- `--json` on `reconcile`, `ack-report`, `tat`, `summary`, `lint`,
  `doctor` and the `profile` commands.
- `-v`, `-q`, `--debug`, accepted before or after the command; grouped
  `--help` and examples for every command; every command says which
  profile folder and database it used.
- `HL7BRIDGE_FHIR_TOKEN` (bearer token) and `HL7BRIDGE_FHIR_BASE` as the
  default FHIR server.
- Clear messages for a taken port, a port that needs admin rights, an
  address not on this machine, a FHIR server that does not answer, no
  `.hl7` files, LF line ends (converted, with a note), AE and AR ACKs
  (code in words, MSA-3, ERR, quarantine reason, next command), no ACK in
  time, a refused connection, and an old database schema.
- `Dockerfile` (multi-stage, wheel built with uv, dependencies installed
  from `uv.lock` with hashes, non-root user, health check, base images
  pinned by digest) and `compose.demo.yaml` (HAPI with no host port, the
  listener on 127.0.0.1 only, the demo against HAPI, named volumes), plus
  `compose.hapi-port.yaml` to browse HAPI on 127.0.0.1.
- `tools/check_ports.py`: fails on any published port that is not bound
  to 127.0.0.1, in compose files (found by content, long and short port
  forms), workflow service and job containers, and `docker run`,
  `docker create`, `podman run` and `docker compose run` lines, including
  clustered flags such as `-dp`, host networking and publish-all.
- `tools/check_licenses.py`: checks every runtime dependency's license
  against an allow list. Without a `License-Expression`, the `License`
  field and every license classifier must all be allowed; an unknown one
  fails. CI runs it on every OS in the test matrix.
- `docs/cli.md`: commands, defaults, exit codes and error messages.

### Changed

- CI runs once per change: pushes to `main` and pull requests, not both.
- CI pins uv by version, pip, build and hatchling by hash, and the HAPI
  image by digest. The wheel job no longer checks a hard-coded file count;
  it compares lint's JSON with the files the generator wrote, and runs
  `init`, `doctor` and `demo` from the installed wheel.
- New CI jobs: license and port checks, the reference check on commit
  messages, the README quickstart, and a Docker job that builds the image,
  checks the user is not root and runs the compose demo.
- Every CI job has a timeout (15 to 30 minutes), and the HAPI service
  container in CI is published on 127.0.0.1 only.
- The image builds its wheel with hatchling from the hashed build lock
  (`uv build --build-constraints ... --require-hashes`).
- The compose demo no longer keeps its data in a volume; it uses a
  temporary folder in its container, so `up` can run again and again.

### Fixed

- `listen` prints its banner only after the socket is bound, names the
  real cause of a bind failure, and no longer labels a later error as a
  bind failure.
- `lint` reports a file that is not UTF-8 as `HL7-001` instead of
  stopping with an internal error.

### Security

- User names and passwords in a FHIR URL are never printed, including in a
  mistyped URL where the parser puts them in the path.
- `HL7BRIDGE_FHIR_APIKEY` and `HL7BRIDGE_FHIR_TOKEN` go only to a server
  you named, with a warning when they would go over plain http to another
  machine.
- Text a remote party wrote (MSA-3 and ERR in `send`, the ACK reason in
  `ack-report`, the server software name in `fhir probe`) is printed with
  control characters replaced, so a listener cannot move the cursor, set
  the terminal title or hide lines. `send` shows a quarantine reason only
  for hl7bridge's own reason codes.
- `--json` output lists every field by name for each report, so a field
  added to a report later does not reach JSON by accident.

## [0.2.0] - Unreleased

### Security

- Patient identifiers no longer reach logs or error text. The CLI holds the
  `httpx`, `httpcore` and `pydicom` loggers at WARNING. HTTP client records,
  from `httpx`, `httpcore` and every logger below them, are rebuilt from a
  safe field list (method, URL with no query or user info, HTTP version,
  status, trace event name), so reason phrases and headers such as
  `Location` are never printed, even at DEBUG. The filter sits on those
  loggers and on the CLI's handler. Every other record loses URL queries
  and user info, its traceback included.
- Pipeline log lines carry the store row id instead of MSH-10. MSH-10 is
  not logged in any form. Sender-written message types outside the
  hospital profile are not logged.
- The CLI has one error boundary: each failure prints a single line, with
  no chained cause. Errors whose text can quote input print the class name
  and stack frames only. Loader and FHIR client errors are raised without
  a chained cause.
- pydicom's validation messages (logging and `warnings`) are captured while
  a DICOM file is read and turned into a loader error that names the
  hospital folder and file position, not the value or the file name.
- ACKs for rejected messages carry the reason code and the field name in
  MSA-3, never the rejected value. The MSH-9 trigger is echoed only when
  well formed.
- OperationOutcome issue codes outside the FHIR IssueType value set print
  as `unrecognized-code`.
- User names and passwords in a `--fhir-base` URL are never printed.
- `load-outbound` now takes `--config` and rejects lines that name an
  unconfigured hospital. `ack-report` counts outbound reports for a
  hospital no longer configured in an `UNCONFIGURED HOSPITAL` line instead
  of failing with the control id in the message.
- The listener answers AR even when the quarantine write fails too, and
  logs only type names for both failures.
- `FhirPostError` text holds the status, the URL path and a fixed reason. The
  URL query and the server body are kept on the object, never in the
  message. The CLI prints OperationOutcome issue codes only.
- Parse errors log a fixed cause code and the byte length; internal errors
  log the exception type and stack frames, not the exception message. The
  ACK for an unparseable message carries fixed text.
- FHIR sync logs skipped rows as counts per kind and reason. The keys stay
  in the report on stdout.
- Loader errors no longer quote the values they rejected.
- `scripts/demo.sh` publishes HAPI on 127.0.0.1 only, and deletes its data
  folder only if the folder holds the marker file the script wrote. It
  creates that folder with a plain `mkdir` before marking it, refuses `..`
  components and symlinks with trailing slashes, compares protected folders
  by file identity, and stops if the MLLP port is taken or an unknown FHIR
  server answers at the default address.

### Added

- `hl7bridge --version`.
- A clear error when `serve` cannot bind its port.
- `tools/check_references.py`, run in CI, and `docs/principles.md`,
  `docs/testing.md`.
- `LICENSE` (Apache-2.0), `NOTICE`, `CONTRIBUTING.md`, `SECURITY.md`, issue
  and pull request templates.
- CI: checks on Ubuntu; tests on Ubuntu and Windows for every push and
  pull request, plus macOS for pushes to `main` and manual runs; the demo
  guard tests may not skip in CI.

### Changed

- The distribution is now named `hl7bridge`, matching the import package and
  the CLI. It was `hl7-imaging-bridge`.
- Environment variables are renamed from `HL7B_*` to `HL7BRIDGE_*`:
  `HL7BRIDGE_FHIR_APIKEY`, `HL7BRIDGE_FHIR_BASE`, `HL7BRIDGE_FHIR_REQUIRED`.
- The version is read from `hl7bridge.__version__`.
- If you ran an older `scripts/demo.sh`, delete `data/demo` once by hand: it
  has no marker file, so the new script will not delete it for you.

### Fixed

- Lint report keys use forward slashes on every OS. This was the one test
  failure on Windows.
- `.hl7` files are read and written as bytes, so the bare CR segment
  terminator survives on Windows. `.gitattributes` keeps HL7 samples byte for
  byte in Git.
- `tzdata` is a declared dependency on Windows, which has no system time zone
  database.

## [0.1.0]

First working version, never published.

- MLLP listener with an explicit AA, AE or AR decision for every message,
  and a reason-coded quarantine.
- Order-to-image reconciliation, report delivery and ACK tracking,
  monthly turnaround-time reporting, and per-hospital message linting.
- Batch v2-to-FHIR sync with conditional updates, and a read-only
  `fhir-probe` with optional `apikey` header auth.
- Deterministic synthetic generator with a labeled defect manifest, and
  system tests asserted against it.
