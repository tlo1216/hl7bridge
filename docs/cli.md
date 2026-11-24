# Command line

`hl7bridge --help` lists every command by group. `hl7bridge <command>
--help` shows its options and examples.

## Commands

| Group | Command | What it does |
| --- | --- | --- |
| Try it | `demo` | Run the whole pipeline on synthetic data in one command |
| Try it | `init [FOLDER]` | Write the bundled profiles and sample messages into a folder (default `./hl7bridge`) |
| Try it | `doctor` | Check Python, profiles, time zones, database, port and the FHIR server |
| Messages | `explain [PATH]` | Print a message one field per line with field names, components and code meanings; `--all` shows empty fields; `-` reads stdin |
| Messages | `lint [PATH]` | Check messages against the hospital profiles |
| Messages | `send [PATH]` | Send a file, a folder or stdin (`-`) over MLLP, one frame per message, and show the ACKs; `--repeat N`, `--rate PER_SECOND`, `--expect CODE` |
| Messages | `replay` | Resend quarantined (default) or stored (`--from store`) messages, oldest first, byte for byte and each distinct one once; reads a version 2 or 3 store; `--only-reason`, `--since`, `--hospital`, `--limit`, `--dry-run` |
| Messages | `synth --out DIR` | Generate a synthetic day with a defect manifest |
| Messages | `from-synthea PATH --hospital ID --out DIR` | Write radiology ADT, ORM and ORU messages for every ImagingStudy in Synthea FHIR Bundles, offline, plus `index.json`; `--seed N` |
| Listener | `listen` | Run the MLLP listener into the store (`--tls-cert`, `--tls-key`, `--tls-ca` for TLS); `--target fhir` writes each message to a FHIR server as one transaction and ACKs after the commit (see below) |
| Reports | `reconcile` | Orders versus imaging studies |
| Reports | `ack-report` | Delivery status of outbound reports |
| Reports | `tat --month YYYY-MM` | Turnaround-time statistics |
| Reports | `summary` | Message and quarantine counters |
| FHIR | `to-fhir [PATH]` | Convert messages to FHIR transaction Bundles: print one, `--out DIR` for several, `--post` to send them (`--skip-failures` to post when some do not map) |
| FHIR | `fhir sync` | Post the store to a FHIR server, rebuilt from the stored messages as `to-fhir --post` would |
| FHIR | `fhir reset --run-id NAME` | Delete one lab run's Observations, DiagnosticReports, ServiceRequests and Encounters; dry run unless `--yes` and the run id typed back; never a Patient |
| FHIR | `from-fhir --patient ID` | Write sample A04, A08, ORM and ORU messages for a Patient on a FHIR server (`--report ID`: an ORU for a DiagnosticReport) |
| FHIR | `fhir probe` | Read-only server check |
| Data | `load studies --dicom DIR` | Load DICOM study metadata |
| Data | `load outbound --path FILE` | Load the outbound report log |
| Data | `profile show [HOSPITAL ...]` | Print effective profile values, defaults filled in |
| Data | `profile check [DIR]` | Validate a folder of profiles |

Every report command, and `lint`, `doctor` and the `profile` commands,
take `--json`. JSON goes to stdout; status lines go to stderr.

## MLLP over TLS

Off by default; a listener started without these flags speaks plain MLLP.

| Command | Flag | Meaning |
| --- | --- | --- |
| `listen` | `--tls-cert FILE`, `--tls-key FILE` | Serve TLS 1.2 or newer. Both are needed together |
| `listen` | `--tls-ca FILE` | Also require a client certificate that chains to this CA |
| `listen` | `--max-connections N` | With TLS, at most N connections open at once (default 256) |
| `listen` | `--max-connections-per-address N` | With TLS, at most N of those from one address (default 32) |
| `listen` | `--max-pending-per-address N` | With TLS, at most N handshakes pending at once from one address (default 4) |
| `send`, `replay` | `--tls` | Use TLS with the system CA store (on Windows the CA and ROOT stores; see below) |
| `send`, `replay` | `--tls-ca FILE` | Trust only this CA for the listener's certificate (implies `--tls`) |
| `send`, `replay` | `--tls-cert FILE`, `--tls-key FILE` | Present a client certificate (implies `--tls`; both together) |

Senders always verify the certificate chain, its dates and that `--host`
matches a subject alternative name of the certificate (the common name is
never used); there is no flag to turn that off. With TLS the
sender's not-loopback warning is not shown. An empty flag value (an unset
shell variable) is a usage error, never plain MLLP. The private key must be
unencrypted; an encrypted key is refused with a message, never a prompt.
An empty `--host` (an unset shell variable) is a usage error for `send`,
`replay` and `listen`, before any connection or bind: it would otherwise reach
this machine without a host name check, or bind every interface.
TLS errors name the flag (`--tls-key`, `--tls-cert`, `--tls-ca`), never its
value: a value can be key text.

`--tls-ca` is the only trust anchor when given, and it may be an issuing
(intermediate) CA on its own: partial-chain verification is switched on only
then. Without it the system store applies. On Windows that is the certificates
Python imports from the CA and ROOT stores. Neither `--tls` nor `--tls-ca`
consults the Disallowed store (certificates an administrator has explicitly
blocked), so a blocked certificate can still pass if its name, dates and chain
are valid. `--tls-ca` narrows which issuers are trusted; it does not enforce
those blocks.

`--timeout` is one deadline per phase: connecting (name lookup, TCP and the TLS
handshake together), then each message (writing it and waiting for its ACK
together). The reported value is the one that applied. A name lookup that does
not return is abandoned at the deadline; it does not hold the exit.

An ACK counts only when it answers the message sent: the reply must start with
MSH, hold exactly one MSH and one MSA, have MSH-9 `ACK` and an MSA-1 that is
exactly one ACK code (AA, AE, AR, CA, CE, CR; the whole field, no components
or repeats), and its MSA-2 must be the outbound MSH-10.
Both are read the same way, the way the listener reads them (leading
whitespace skipped, trimmed, first component, separator and hex escapes
decoded, control characters dropped), so hl7bridge's own listener, which echoes
the decoded id with separators escaped, always matches. Any other escape in
MSA-2 is refused unread, and an MSH that is not UTF-8 names no control id.
`UNKNOWN` (what hl7bridge's listener answers when it cannot read the MSH)
counts only in a negative ACK (AE, AR, CE, CR), never in an AA or CA, even for
a message whose MSH-10 is `UNKNOWN`. `send` refuses before anything is sent
(exit 2) a message with no MSH-10 an ACK can name (empty after that reading,
`UNKNOWN`, not UTF-8, or with another escape), and two different messages
whose MSH-10 reads the same; the same bytes again (`--repeat`) are fine.
`replay` sends stored bytes as they are, also a quarantined message with no
control id; only the listener's negative `UNKNOWN` ACK answers it. A cut-short ACK, a reply that
is not one ACK, or another message's ACK ends the run as an error (exit 3) with
the ACKs already confirmed kept in the results; it is never counted. The `sent`
total counts frames written, not messages queued.

A listener with TLS but no `--tls-ca` on a non-loopback `--host` prints
`warning: ... TLS encrypts the traffic but does not authenticate senders`:
anyone who can reach the port can still send. Use `--tls-ca` (mutual TLS).
The listener logs a failed handshake with the peer address and the reason
(including the certificate check text), at most once a minute for each peer
address, followed by a summary count, and at most 20 such lines a minute across
all peers (the rest are counted in one line); it counts all of them and logs the
total when it stops cleanly (Ctrl+C, SIGTERM on Linux and macOS, or an error).
SIGTERM takes the same path as Ctrl+C. On Windows only Ctrl+C is a clean stop:
ordinary process termination (a service stop, `taskkill`, `Popen.terminate()`,
which is `TerminateProcess`) ends the process at once, runs no Python code, and
writes neither the total nor any shutdown line. On a clean stop idle clients
are closed immediately, a client still in its handshake is cancelled, and a
client whose message is still being handled gets 5 seconds, then is cut. With
`--target fhir` the listener then waits up to 5 more seconds for its FHIR
workers. A worker still busy is left to finish its message and write its log
row: the FHIR client and the direct-mode log stay open for it, and the process
exits only when it is done. That can take longer than `--fhir-timeout`: it is
an httpx timeout, so it bounds each phase of each request (connecting, each
read, each write, waiting for a pooled connection) on its own, not a whole
request and not the several requests one message can need. A repeated SIGTERM
is ignored while the event loop runs (the client grace above); after it
returns, a second SIGTERM can stop the wait for the FHIR workers. At most
`--max-connections` TLS connections (default 256) may be open at once, at most
`--max-connections-per-address` of those from one address (default 32), at most
64 may wait to finish a handshake, and at most `--max-pending-per-address` of
those from one address (default 4);
more are closed, with one log line a minute. The per-peer failure table keeps
1024 peers. For each accepted client certificate it logs one line with the
peer, the SHA-256 fingerprint and serial, every value percent-encoded
so it holds no space or `=` and cannot start another field; this line is
written at a level that `--quiet` does not hide. The subject, subject
alternative names and issuer, which can hold a person's name, an email address
or a URI query (a self-issued certificate's issuer is its subject), are logged
only at DEBUG (`-vv`). `--timeout` must be a finite number
above 0. Failures:

| Case | Message |
| --- | --- |
| Certificate refused | `error: the TLS certificate from 127.0.0.1:2575 was refused: <reason>. Nothing was sent.` (exit 3) |
| Never connected (refused, no route, unknown host, timed out) | `error: cannot connect to 127.0.0.1:2575: connection refused. Never connected, nothing was sent.` (exit 3), the same with or without TLS |
| Handshake failed | `error: the TLS handshake with 127.0.0.1:2575 failed: <reason>. Nothing was sent.` (exit 3) |
| Handshake stalled (only when the deadline passed; a reset is "Handshake failed") | `error: no TLS handshake with 127.0.0.1:2575 within 10 s. Nothing was sent.` (exit 3) |
| Broke after the handshake | `error: the connection to 127.0.0.1:2575 broke (SSLError) after 1 of 2 ACKs. The message after the last ACK may or may not have been processed.` (exit 3); the first message without an ACK is listed as `no ACK (it may or may not have been processed)`, the rest as `not sent` |
| Encrypted key | `error: the TLS server private key given with --tls-key is encrypted. Give an unencrypted key ...` (exit 2) |
| Wrong or cut-short ACK | `error: the reply from 127.0.0.1:2575 was not a valid ACK for the message sent (...) after 1 of 2 ACKs. ...` (exit 3) |
| Empty host | `error: --host is empty.` (exit 2) |
| No usable MSH-10 | `error: a.hl7 has no usable MSH-10 (control id): it is empty, UNKNOWN, not UTF-8, or holds an escape other than a separator or hex escape; nothing sent.` (exit 2) |
| Shared MSH-10 | `error: b.hl7 has the same MSH-10 (control id) as an earlier message with other content; nothing sent.` (exit 2) |
| Empty flag value | `error: --tls-ca is empty.` (exit 2) |
| Missing or bad file | `error: cannot read the TLS CA file given with --tls-ca.` (exit 2) |
| One of a pair given | `error: TLS needs both --tls-cert and --tls-key.` or `error: --tls-cert and --tls-key go together.` (exit 2) |

## Direct to FHIR: `listen --target fhir`

```
hl7bridge listen --target fhir --fhir-base http://127.0.0.1:8080/fhir --run-id lab-a --config lab-profiles
```

Each message becomes one FHIR transaction Bundle, posted before the ACK
goes back. AA means the server committed all of it: the answer was a
`transaction-response` Bundle with one entry per request and every entry
200 or 201. Anything less is AE, and the sender keeps the message and
sends it again; every write is keyed, so a resend updates in place. The
full ACK table is in `src/hl7bridge/ingest/direct.py` and ADR-0004.

- **Patients are read only.** Every loaded profile must say
  `fhir.patient_policy: match_only`. The listener finds the Patient by
  MRN (PID-3, its system from `mrn_by_authority` by PID-3.4, or `mrn`
  when PID-3.4 is empty; a PID-3.4 the profile does not map is AE
  `unknown_assigning_authority`). None, several (counted over every
  result page), an inactive one or one replaced by another is AE. A key
  that already belongs to another Patient is AE `patient_mismatch`: a
  report never moves between patients. ADT^A08 and ADT^A40 get AR
  `patient_read_only`. Supported: ADT^A01, ADT^A04 (an Encounter), ORM^O01
  and ORU^R01.
- **A run id is required** (`--run-id` or `HL7BRIDGE_RUN_ID`, 1 to 32
  letters, digits or `-`). Every identifier written gets the system
  `<profile system>/run/<run id>`, the value from the message stays as a
  second identifier under `<profile system>/as-sent` with `use:
  secondary` (no `to-fhir --post` or `fhir sync` search can match it), and
  every resource carries
  the lab tag (`fhir.tag`) and the run tag (`<lab tag system>/lab-run`,
  code = run id). A key that matches a resource without this run's tag is
  AE `outside_run` and nothing is written.
- **One writer per key.** While a transaction is in flight the listener
  holds a lock per key, and per message identity, so two copies of one
  message on two connections run one after the other. A resource a
  search did not find is made by a conditional create that must answer
  201; one found is written by id with `If-Match`.
- **Message identity** is hospital, MSH-3, MSH-4 and MSH-10 within the
  run, plus a hash of the whole message. The first hash committed, or
  maybe committed (a timeout, a lost or malformed answer, a 5xx), holds
  the identity: anything else under it, a restamped MSH-7 included, gets
  AE `control_id_conflict`. The same bytes again get AA and write nothing
  when each of their keys is still on the server in the run; after a
  reset or on a wiped server they are written again. Log rows are bound
  to the FHIR base they were answered for.
- **MSH-11** must be in `fhir.allowed_processing_ids` (default T and D):
  production traffic (P) gets AR.
- **A late older report** (by OBR-22, then OBR-25) gets AE `stale_result`
  and writes nothing. `to-fhir --post` and `fhir sync` refuse it too. A
  retraction (cancelled, entered-in-error) outranks a correction. With no
  OBR-22 on either side and the same status the message is taken as the
  newer one: MSH-7 is not compared (ADR-0004).
- The log of every arrival, which is also the quarantine (AE and AR rows
  keep their bytes, AA rows none), is its own SQLite file: `--db`, default
  `./hl7bridge-direct.db`. It is never a store database, and it is held
  exclusively while the listener runs, so a second listener on the same
  file is refused. If a row cannot be written the answer is AE
  `log_write_failed`, never AR, so the sender keeps the message.
- `--fhir-timeout SECONDS` (default 10) bounds each FHIR request; a
  timeout is AE. Each message also has a 60 second deadline from arrival
  to the commit: lock waits and the steps before the commit stop there
  with AE `deadline_exceeded` and nothing sent. The FHIR calls run on a
  pool of 8 worker threads of the listener's own, so one slow request
  does not hold up other connections.
- Every value echoed into the ACK (MSH-3, MSH-4, MSH-10, MSH-12) is
  HL7-escaped and loses its control characters, so a sender cannot add a
  segment or a field to the ACK. The store listener does the same.
- `--mapping-module FILE` loads mapping hooks (below); it is refused when
  `--host` is not loopback.

## Lab runs: `fhir reset` and `from-fhir`

`hl7bridge fhir reset --run-id lab-a` lists how many resources of each type
carry run `lab-a`'s tag and deletes nothing. With `--yes` it asks for the
run id to be typed back, then deletes them all in one transaction: if the
server refuses any delete, none happens and the command exits 1. Each
delete is conditional on the id and the run tag, so a resource retagged
to another run after the count is kept. It never searches or deletes a
Patient, and there is no scope wider than one run. `--db FILE` (the
listener's direct-mode log, with the listener stopped) also voids the
run's log rows there, so a message under an old control id is taken as
new.

`hl7bridge from-fhir --patient 123 --hospital WINTEHR --out msgs` reads one
Patient and writes `01_adt_a04.hl7`, `02_adt_a08.hl7`, `03_orm_o01.hl7` and
`04_oru_r01.hl7` with its MRN in PID-3. The MRN comes only from an
identifier under the profile's `mrn` system or one `mrn_by_authority`
maps (the one typed MR among those, else the first; the command says
which), and PID-3.4 is that authority, or empty for `mrn`; a Patient
with neither is refused. A `birthDate` that is not a FHIR date is
refused, and every value loses its control characters. MSH-10, the
accession and the visit number carry a short hash of the Patient id, so
two patients rendered in one second do not collide. MSH-11 is T. Files
are never overwritten.
`--report 456` writes one ORU^R01 for a DiagnosticReport and its
Observations instead.

## Radiology messages from Synthea: `from-synthea`

`hl7bridge from-synthea synthea/output/fhir --hospital WINTEHR --out msgs`
reads Synthea FHIR R4 Bundles (one file, or every `*.json` under a folder)
and writes the feed a radiology system would send for each ImagingStudy:
an `ADT^A04` once per encounter (`ADT^A01` for an inpatient encounter,
when the profile expects it), an `ORM^O01` new order and an `ORU^R01`
report. It reads nothing over the network and never looks at the clock:
the same Bundles, `--seed` and profile give the same bytes.

- PID follows the `from-fhir` rules: the MRN only from an identifier under
  the profile's `mrn` system or one `mrn_by_authority` maps (Synthea's MR
  identifier uses `http://hospital.smarthealthit.org`). A Patient with
  neither stops the run. Input files are named by their position in sorted
  path order (`file 3`), never by name, since Synthea names each file after
  its patient; errors and the status lines say `PATH` and `--out`, never the
  paths. A folder linked into PATH (a symlink, or a junction on Windows) is
  not entered; a linked file is read.
- The profile must expect `ADT^A04`, `ORM^O01` and `ORU^R01`, allow MSH-11
  `T`, have no HL7 separator in its first facility code or `hl7_version`,
  and have no `mrn_by_authority` authority holding `&`, a control character
  or edge spaces (the direct listener could not read it back from PID-3.4).
- The order code, priority and order time come from a ServiceRequest the
  study's `basedOn` names, else the study's `procedureCode`. The report text
  and status come from a DiagnosticReport whose `imagingStudy` names the
  study (or whose `basedOn` names its ServiceRequest); without one the
  report is a short normal-study text ending in a line that says it is
  synthetic. Synthea's clinical-note reports are never used. A linked
  Encounter, ServiceRequest or DiagnosticReport whose `subject` is another
  Patient skips the study.
- Accession, placer and visit numbers are 16-character hashes of the MRN
  (its system and value) and the source resource; the seed changes only
  times, report lines and MSH-10 (which also changes whenever any part of a
  message, MSH included, does). ORC and OBR carry the same placer and filler
  numbers: the accession is the placer number in both when the profile reads
  it from ORC-2 or OBR-2, the filler number in both for ORC-3 or OBR-3. A study
  with no Encounter is its own outpatient visit. A procedure with no code
  gets a local one (`IMGCT^CT imaging study^99HUB`).
- Files are numbered in feed order (`00001_adt_a04.hl7`, ...), so
  `hl7bridge send msgs` replays them in order. `index.json` lists each
  study's source file, ImagingStudy (its UUID id, else its position such
  as `file 3 entry 7`, never a fullUrl), study UID (only from a
  `urn:dicom:uid` identifier or Synthea's `urn:oid:` one), MRN and its
  system, accession, visit and files, and each skipped study with its
  reason; it holds no names or birth dates. A copy of a study already
  written (the same MRN and ImagingStudy) is skipped as a duplicate; a copy
  skipped for its own reason never blocks a later good one.
- `--out` must not exist yet (an existing folder, even an empty one, a
  link or a junction is refused) and may not be inside PATH (links and
  junctions resolved); both are checked before any Bundle is read. Its
  parent folder must be writable, since the run is staged there: in Docker,
  mount the parent of `--out`, not `--out` itself. Everything is rendered
  and checked before the first file is written. The files go into a new
  hidden `.from-synthea-*` folder beside `--out` that is renamed to `--out`
  in one step when all are written, so a failed write leaves no partial
  output in `--out`. On Linux and macOS the output gets the usual umask
  modes; on Windows it keeps the owner-only access that Python gives a new
  temporary folder. A study
  whose messages the listener would not accept is skipped and listed (no
  modality, a time with no UTC offset or outside 1900 to 2100, times out of
  order, a partial or null `birthDate`, a padded MRN, a report status with
  no ORU result status P, F or C, a Patient not in FHIR shape, a time in a
  repeated local hour when the profile writes no offset, a local UTC offset
  with seconds (local mean time, before about 1972) when it writes one,
  and so on); the rest is written and the command exits 1.
- Limits: 64 MiB per Bundle file, 20,000 files and 10,000 studies per run,
  32 KiB and 499 lines of report text per study (500 OBX with the synthetic
  line), 64 KiB per message.
- Known limits: on Linux and macOS, an empty folder another program makes
  at `--out` in the instant between the last write and the rename is
  replaced by the run (a folder with anything in it is refused). On
  Windows a virus scanner that still holds a new file can make the rename
  fail; nothing is written and a rerun works. If cleanup after a failure
  cannot remove a staged file (one held open), a hidden `.from-synthea-*`
  folder stays beside `--out`. On a case-insensitive macOS volume the
  inside-PATH check compares names case-sensitively. A file linked into
  PATH is read; only linked folders are skipped.

For a teaching lab, run it on the lab's own Synthea export with the lab
profile, then `hl7bridge send msgs` to `listen --target fhir`: the patients
already exist on the server, so each study becomes an Encounter,
ServiceRequest and DiagnosticReport under the run tag.

## Mapping hooks

`--mapping-module FILE` (on `listen --target fhir` and `to-fhir`) loads a
Python file that may define `sex_code(pid8)`, `abnormal_flag(obx8)` and
`observation_time(obx14, zone)`. Each replaces one mapping step; what it
returns is checked, and an exception (SystemExit included) or a value
outside the FHIR value set is a mapping error naming the field. Any other
public function in the file is refused, and so is a hook name bound to
anything but a `def` in the file (a `functools.partial`, an import). The
file runs as code inside hl7bridge: use it for a loopback teaching lab,
never a shared listener; `listen --target fhir` refuses it off loopback.
The command warns on stderr when it loads one.

`to-fhir --post` and `fhir sync` create and merge Patients, so they refuse
a `match_only` profile.

### Removed in 0.4.0

The names renamed in 0.3.0 kept working through 0.3.x with a warning. They
are gone in 0.4.0 and exit 2 as unknown commands, and `scripts/demo.sh` is
removed.

| Old | New |
| --- | --- |
| `serve` | `listen` |
| `fhir-sync` | `fhir sync` |
| `fhir-probe` | `fhir probe` |
| `load-studies` | `load studies` |
| `load-outbound` | `load outbound` |
| `lint --dir D`, `send --dir D` | `lint D`, `send D` (the option is still accepted) |

## Global options

Accepted before or after the command.

| Option | Effect |
| --- | --- |
| `--version` | Print the version |
| `-v`, `-vv` | INFO, then DEBUG log output on stderr (`listen` logs at INFO by default) |
| `-q` | Errors only on stderr; no `using ...` lines |
| `--debug` | DEBUG logs, and stack frames after an error. It never prints a value read from a message or a server |

## Defaults

| Setting | Lookup order |
| --- | --- |
| Profile folder | `--config`, `HL7BRIDGE_CONFIG`, `./hl7bridge/profiles`, the bundled profiles |
| Database | `--db`, `HL7BRIDGE_DB`, `./hl7bridge.db` |
| FHIR server | `--fhir-base`, then `HL7BRIDGE_FHIR_BASE`. `fhir probe` and `doctor` fall back to `http://localhost:8080/fhir`; `fhir sync` and `to-fhir --post` write, so they need one of the two; `demo` uses only `--fhir-base` |
| FHIR credentials go to | only a server named with `--fhir-base` or `HL7BRIDGE_FHIR_BASE`, never the fallback |
| FHIR credentials | `HL7BRIDGE_FHIR_APIKEY` (an `apikey` header), `HL7BRIDGE_FHIR_TOKEN` (a bearer token), from the environment only |
| Listener | `--host 127.0.0.1 --port 2575`, for `listen`, `send` and `replay` |

Every command that reads profiles or the store prints which folder and
database it used, on stderr. `listen` and `load` create the database if it
is missing; `summary`, `reconcile`, `ack-report`, `tat` and `fhir sync`
open an existing one only, and `replay` opens it read-only. A SQLite file
that holds another program's tables is refused, never changed.

`explain` and `to-fhir` need no database. `to-fhir` with `--out` creates
the folder if needed and creates each file exclusively, so an existing
file is never overwritten.

## What `demo` deletes

Guaranteed:

- Nothing in a folder named with `--data-dir` is ever deleted.
- Deletion happens only inside the folder `tempfile.mkdtemp` made in the
  same run, only to names that run created exclusively, only while each
  name still leads to an object of the recorded kind and file identity,
  never through a link and never recursively. Anything else is left and
  the folder is kept.
- Other users cannot change anything in that folder. On Linux and macOS
  `mkdtemp` makes it with mode 0700 (a test checks this). On Windows the
  default temp folder is in your profile, whose ACL admits only you,
  SYSTEM and administrators.

Not guaranteed: protection against a program that runs as you, or as an
administrator, and writes into that private folder while the demo runs.
It could rename another file onto a recorded name between the last check
and the delete (Linux and macOS), replace a folder between its creation
and the identity read, or, on a file system that reuses file ids (FAT,
some ReFS volumes), make a new file that carries a recorded identity.
Each needs write access to a folder only you can write to, so that
program could already delete your files directly; the race gives it
nothing new. If you set `TMP` or `TEMP` to a shared folder on Windows,
the folder's ACL is whatever that location gives it.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Findings, rejected messages (AE or AR, or any ACK that is not `--expect`), messages that could not be mapped or explained, a Bundle the server did not fully commit, an ADT^A40 refused by `--post` (`to-fhir`), or a merge or create another writer got to first (send again), or a failed check |
| 2 | Usage or configuration error: bad arguments, a bad profile, a path or database that cannot be used, a port that cannot be bound |
| 3 | Connection error: a listener or server did not answer, or no ACK came in time |
| 4 | Internal error: a bug in hl7bridge. Run again with `--debug` and report it |
| 130 | Interrupted with Ctrl+C. `listen` is the exception: Ctrl+C is how you stop it, so it prints `listener stopped` and exits 0 |

## Error messages

A user error never shows a traceback. It prints one `error:` line that
says what failed, where, and what to try. The message never quotes a value
read from a message, a DICOM file or a server answer; errors whose text
could do that print only their class name.

| Situation | Message |
| --- | --- |
| Profile has an unknown key | `error: hospb.yaml: unknown key 'acession_field'. Did you mean 'accession_field'?` |
| Port in use | `error: cannot listen on 127.0.0.1:2575, the port is in use. Try --port 2576.` |
| Port needs admin rights | `error: cannot listen on 127.0.0.1:80, permission denied. Ports below 1024 need admin rights. Try --port 81.` |
| Address not on this machine | `error: cannot listen on 10.9.9.9:2575, that address is not on this machine. Try --host 127.0.0.1.` |
| Listener stops after it started | `error: the listener on 127.0.0.1:2575 stopped: <reason>.` |
| FHIR server down | `error: no answer from http://localhost:8080/fhir/metadata. Is the server running? Try: hl7bridge doctor` |
| No `.hl7` files | `error: no *.hl7 files under ./msgs. Try: hl7bridge init` |
| LF line ends in a file | A note on stdout, not an error: `note: a.hl7 uses LF line ends; converted to CR for sending.` |
| ACK is AE or AR | On stdout: the code in words, the quarantine reason when MSA-3 starts with one of hl7bridge's own reason codes, and `next: hl7bridge lint <file>`. The receiver's own MSA-3 and ERR text is not printed (it can quote a patient identifier); add `--show-ack-text` to print it for every negative ACK (AE, AR, CE, CR), also one `--expect` names; never for AA or CA (control characters replaced by `?`, length capped) |
| No ACK in time | `error: no ACK from 127.0.0.1:2575 within 10 s. The message may or may not have been processed.` (exit 3) |
| Listener not running | `error: cannot connect to 127.0.0.1:2575: connection refused. Never connected, nothing was sent. Is the listener running? Try: hl7bridge listen` |
| Old database schema | `error: database hl7bridge.db has schema version 1; this hl7bridge needs version 3. There is no migration. Demo databases can be deleted and made again.` |
| Not a database | `error: notes.txt is not an hl7bridge database (DatabaseError). Pass --db with another path, or delete the file if it is a demo database.` |
| Demo folder not empty | `error: refusing to use data: it is not empty. The demo writes only into a new or empty folder, and never deletes anything in a folder you name. Pass --data-dir with a new folder, or leave it out to use a temporary folder.` |
| No database | `error: no database at hl7bridge.db. Run hl7bridge listen or hl7bridge load first, or pass --db with the database file.` |
| Another program's database | `error: notes.sqlite is a SQLite database that hl7bridge did not create. Pass --db with another path.` |
| `fhir sync` with no server | `error: fhir sync writes to a FHIR server, so it needs one named. Pass --fhir-base URL or set HL7BRIDGE_FHIR_BASE.` |
| Bad FHIR URL | `error: --fhir-base is not an http(s) URL with a host (value not shown). Try: --fhir-base http://localhost:8080/fhir` |
| Bad month | The usage line, then `error: argument --month: '2026-13' is not a month in the form YYYY-MM` (exit 2) |
| Missing input file | `error: --path missing.jsonl is not a file.` (and `--dicom DIR is not a folder.`) |
| Not loopback (`send`, `replay`) | `warning: sending to 192.0.2.10, which is not loopback. MLLP has no encryption; anyone on the network path can read these messages.` |
| `send --expect AE` | A summary line `  expected AE: 1 of 1`; exit 1 when any ACK differs. Each ACK other than the expected code is listed |
| `send`, input not HL7 | `error: x.hl7 does not start with an MSH segment; nothing sent.` |
| Replay of a retried message | `skipping 1 duplicate(s): the same bytes as an older row` (the oldest copy is sent) |
| Bad `--since` | `error: --since 'yesterday' is not an ISO 8601 timestamp.` |
| Message not HL7 (`explain`) | `error: bad.hl7: not parseable as HL7 v2.` (exit 1; nothing from the file is quoted) |
| Mapping refused (`to-fhir`) | `error: a.hl7: PID-8: code not in table 0001 (administrative sex); refusing to guess` (the field position and a fixed reason, never the value; exit 1) |
| Key with a search character | `error: a.hl7: PID-3: holds a character that cannot be part of a FHIR search key` |
| Bad escape | `error: a.hl7: OBX-5: holds a malformed escape sequence` |
| Not UTF-8 (`to-fhir`) | `error: bad.hl7: not valid UTF-8; not converted.` (`explain` shows such bytes as U+FFFD instead) |
| `--post` with a failure | `nothing posted: 1 message(s) could not be mapped; fix them, or pass --skip-failures to post the rest.` (exit 1) |
| Older report after a newer one | `error: b.hl7 holds an older version of the same report as a.hl7 (OBR-22, then OBR-25); posting it after would undo the newer one. Post the messages in the order they were sent, or leave the older one out.` (nothing posted) |
| A40 the server contradicts (`to-fhir --post`) | `error: FHIR request to Patient failed: the merged-away Patient is already replaced by another Patient; a contradicting merge is not applied` (nothing posted; exit 1; `fhir sync` skips it loudly instead) |
| Another writer created a Patient mid-merge | `error: FHIR request to Patient failed: another writer created the Patient while the merge was posted; the merge may be only partly applied (the other Patient may already be updated, or the link missing); send again (a resend is safe)` (exit 1) |
| Several messages, no `--out` | `error: 15 messages need a folder for their Bundles. Pass --out DIR, or --post to send them.` |
| Bundle file exists | `error: bundles/a.json already exists; to-fhir never overwrites a file. Pass --out with a new folder.` |
| `to-fhir --post` with no server | `error: to-fhir --post writes to a FHIR server, so it needs one named. Pass --fhir-base URL or set HL7BRIDGE_FHIR_BASE.` |
| Bundle not fully committed | `error: FHIR server returned 200 for /fhir: entry 4 was not committed` (also for an answer that is not a `transaction-response`, or has the wrong number of entries; exit 1, and later Bundles are not sent) |
| Replay filter | `error: --only-reason applies to --from quarantine only.` and `error: --only-reason takes one of hl7bridge's quarantine reason codes. Known: ...` |
| `from-synthea --out` not empty | `error: --out is not empty; from-synthea never mixes two runs. Pick a new folder.` (PATH and `--out` are named by option, never by path) |
| `from-synthea`, a file that is not a Bundle | `error: file 1 is not a FHIR Bundle. Point PATH at output/fhir.` |
| File system error | `error: FileNotFoundError: No such file or directory. Check the paths you passed.` (exit 2; the file name is not printed, since it can be a name read from a folder) |

Each row has a test on its text in `tests/unit/test_cli.py`,
`tests/unit/test_cli_messages.py`, `tests/unit/test_fhir_sync_mapping.py`,
`tests/unit/test_from_synthea.py`,
`tests/unit/test_config.py` or `tests/unit/test_demo.py`.
