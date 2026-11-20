# ADR-0004: Direct mode writes one transaction per message, ACK after commit

Date: 2026-11-20
Status: accepted

## Context

`listen --target fhir` takes HL7 v2 from a sender and writes it straight to
a FHIR server, with no store in between. It is built for a teaching lab:
many students or sessions (runs) send into one server whose patients
already exist and must not change. ADR-0003 chose conditional updates for
the batch path, where a failed run can simply be run again. Here the sender
deletes its copy when it gets AA, so AA has to mean the data is on the
server, all of it.

## Decisions

1. **One message, one transaction Bundle, then the ACK.** AA only after a
   valid transaction response: a Bundle of type `transaction-response` with
   one entry per request entry, each 200 or 201. A 2xx status alone is not
   enough. A lost answer, a timeout and an unreachable server are AE: the
   listener cannot tell whether the data landed, so it does not claim it,
   and the sender's resend is safe because every write is keyed.
2. **AE for what a resend or a fix can cure, AR for what it cannot.** AR:
   unparseable input, a type this listener does not take (A08 and A40
   among them), a processing id other than the profile allows (P by
   default). AE: mapping errors, patient matching, run ownership, stale
   results, every server failure. The table is `ack_code_for` in
   `ingest/direct.py`, tested row by row.
3. **Patients are matched, never written.** `patient_policy: match_only`
   is required, and the batch path (`to-fhir --post`, `fhir sync`), which
   creates and merges Patients, refuses such a profile. The count of
   matches is over the whole result (`_total=accurate`, the larger of
   `Bundle.total` and the entries, a `next` link meaning more than one);
   one active Patient that no other replaced is the only match. The MRN
   system comes from `mrn_by_authority` by PID-3.4, or `mrn` when PID-3.4
   is empty; a valued authority the profile does not map is AE
   `unknown_assigning_authority`, never the default system. A key whose
   resource has another Patient as subject is AE `patient_mismatch`. A08
   would change a Patient that no run owns, so it is refused rather than
   half applied.
4. **Run ownership.** A run id is required at start. Every identifier
   written is namespaced (`<system>/run/<run id>`), the message's value is
   kept as a secondary identifier under `<system>/as-sent` (a system the
   batch path never searches), and every resource carries the lab tag and
   the run tag. Each key is searched before the write; a match that lacks
   the run tag is AE `outside_run`. `fhir reset --run-id` deletes by the
   run tag only, in one transaction, never a Patient; each delete is
   conditional on the id and the run tag, because HAPI ignores `If-Match`
   on a delete.
5. **Search, then write by id or create once.** A found resource is written
   by id with `If-Match` on the version read (a merge for Encounter and
   ServiceRequest, as on the batch path; a replace for results and
   reports). A missing one is a conditional create (`ifNoneExist` on the
   run identifier and the run tag) whose answer must be 201: 200 means
   another writer made it in between, and the message gets AE
   `concurrent_write` instead of overwriting their version unmerged. The
   batch path's merge entries use the same create-once rule now.
6. **One writer per key in the process.** The listener holds a lock per
   message identity and per resource key, taken in sorted order, while a
   transaction is in flight. Two copies of one message on two connections
   run one after the other and leave one set of resources. A wait ends at
   the message's deadline (60 seconds from arrival to the commit, AE
   `deadline_exceeded`), and the work runs on a bounded pool of the
   listener's own.
7. **Message identity** is hospital, MSH-3, MSH-4 and MSH-10 within the
   run, with a hash of the whole message (segment terminators normalized)
   compared under it. No field is ignored on a resend: a restamped MSH-7
   is a conflict. The store will follow the same rule when it gains
   sending application and facility in its key. The first hash that was
   committed or may have been (timeout, lost or malformed answer, 5xx, a
   lost create race) holds the identity. A logged duplicate gets AA only
   while each of its keys is still on the server in the run; otherwise it
   is written again. Log rows are bound to the FHIR base, and `fhir reset
   --db` voids the run's rows.
8. **Stale results.** An ORU older than the report on the server (OBR-22,
   then OBR-25 status rank, a retraction above a correction) writes
   nothing: AE in direct mode, an error on `to-fhir --post`, a skipped
   row in `fhir sync`. This deviates from the plan, which named MSH-7 as
   the fallback: MSH-7 is not stored on the server, so with no OBR-22 on
   either side and the same rank the message is taken as the newer one.
9. **SQLite only for a log.** Every arrival is a row in a direct-mode log
   file of its own; rows with AE or AR keep their bytes and are the
   quarantine, AA rows keep none. The file is never a store database and
   is held exclusively while open. A failure to write a row is AE
   `log_write_failed`, never AR.

## Consequences

- Locks are in one process. Two listener processes on one log file are
  refused; two on one run with two log files could still race. The
  create-once rule and `If-Match` turn that race into AE, though a
  message whose create lost the race may still have committed its other
  entries (the AE says send again). Run one listener per run.
- Direct mode writes Encounter, ServiceRequest, Observation and
  DiagnosticReport only. Labs that need Patients created use the batch
  path (`to-fhir --post`) on their own server.
- Teaching code can replace three mapping steps through
  `--mapping-module`. It runs as code, so it is for loopback labs only.
