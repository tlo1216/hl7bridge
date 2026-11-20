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
| Listener | `listen` | Run the MLLP listener into the store; `--target fhir` writes each message to a FHIR server as one transaction and ACKs after the commit (see below) |
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
| ACK is AE or AR | On stdout: the code in words, MSA-3 and ERR if present (control characters replaced by `?`, length capped), the quarantine reason when MSA-3 starts with one of hl7bridge's own reason codes, and `next: hl7bridge lint <file>` |
| No ACK in time | `error: no ACK from 127.0.0.1:2575 within 10 s. The message may or may not have been processed.` (exit 3) |
| Listener not running | `error: cannot connect to 127.0.0.1:2575: connection refused. Is the listener running? Try: hl7bridge listen` |
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
| File system error | `error: FileNotFoundError: No such file or directory. Check the paths you passed.` (exit 2; the file name is not printed, since it can be a name read from a folder) |

Each row has a test on its text in `tests/unit/test_cli.py`,
`tests/unit/test_cli_messages.py`, `tests/unit/test_fhir_sync_mapping.py`,
`tests/unit/test_config.py` or `tests/unit/test_demo.py`.
