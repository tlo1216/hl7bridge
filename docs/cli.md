# Command line

`hl7bridge --help` lists every command by group. `hl7bridge <command>
--help` shows its options and examples.

## Commands

| Group | Command | What it does |
| --- | --- | --- |
| Try it | `demo` | Run the whole pipeline on synthetic data in one command |
| Try it | `init [FOLDER]` | Write the bundled profiles and sample messages into a folder (default `./hl7bridge`) |
| Try it | `doctor` | Check Python, profiles, time zones, database, port and the FHIR server |
| Messages | `lint [PATH]` | Check messages against the hospital profiles |
| Messages | `send [PATH]` | Send one file or a folder over MLLP and show the ACKs |
| Messages | `synth --out DIR` | Generate a synthetic day with a defect manifest |
| Listener | `listen` | Run the MLLP listener |
| Reports | `reconcile` | Orders versus imaging studies |
| Reports | `ack-report` | Delivery status of outbound reports |
| Reports | `tat --month YYYY-MM` | Turnaround-time statistics |
| Reports | `summary` | Message and quarantine counters |
| FHIR | `fhir sync` | Post the store to a FHIR server |
| FHIR | `fhir probe` | Read-only server check |
| Data | `load studies --dicom DIR` | Load DICOM study metadata |
| Data | `load outbound --path FILE` | Load the outbound report log |
| Data | `profile show [HOSPITAL ...]` | Print effective profile values, defaults filled in |
| Data | `profile check [DIR]` | Validate a folder of profiles |

Every report command, and `lint`, `doctor` and the `profile` commands,
take `--json`. JSON goes to stdout; status lines go to stderr.

### Renamed in 0.3.0

The old names still work in 0.3.x and print a warning. They go away in
0.4.0.

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
| FHIR server | `--fhir-base`, then `HL7BRIDGE_FHIR_BASE`. `fhir probe` and `doctor` fall back to `http://localhost:8080/fhir`; `fhir sync` writes, so it needs one of the two; `demo` uses only `--fhir-base` |
| FHIR credentials go to | only a server named with `--fhir-base` or `HL7BRIDGE_FHIR_BASE`, never the fallback |
| FHIR credentials | `HL7BRIDGE_FHIR_APIKEY` (an `apikey` header), `HL7BRIDGE_FHIR_TOKEN` (a bearer token), from the environment only |
| Listener | `--host 127.0.0.1 --port 2575` |

Every command that reads profiles or the store prints which folder and
database it used, on stderr. `listen` and `load` create the database if it
is missing; `summary`, `reconcile`, `ack-report`, `tat` and `fhir sync`
open an existing one only. A SQLite file that holds another program's
tables is refused, never changed.

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
| 1 | Findings, rejected messages (AE or AR) or a failed check |
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
| Old database schema | `error: database hl7bridge.db has schema version 1; this hl7bridge needs version 2. There is no migration. Demo databases can be deleted and made again.` |
| Not a database | `error: notes.txt is not an hl7bridge database (DatabaseError). Pass --db with another path, or delete the file if it is a demo database.` |
| Demo folder not empty | `error: refusing to use data: it is not empty. The demo writes only into a new or empty folder, and never deletes anything in a folder you name. Pass --data-dir with a new folder, or leave it out to use a temporary folder.` |
| No database | `error: no database at hl7bridge.db. Run hl7bridge listen or hl7bridge load first, or pass --db with the database file.` |
| Another program's database | `error: notes.sqlite is a SQLite database that hl7bridge did not create. Pass --db with another path.` |
| `fhir sync` with no server | `error: fhir sync writes to a FHIR server, so it needs one named. Pass --fhir-base URL or set HL7BRIDGE_FHIR_BASE.` |
| Bad FHIR URL | `error: --fhir-base is not an http(s) URL with a host (value not shown). Try: --fhir-base http://localhost:8080/fhir` |
| Bad month | The usage line, then `error: argument --month: '2026-13' is not a month in the form YYYY-MM` (exit 2) |
| Missing input file | `error: --path missing.jsonl is not a file.` (and `--dicom DIR is not a folder.`) |
| File system error | `error: FileNotFoundError: No such file or directory. Check the paths you passed.` (exit 2; the file name is not printed, since it can be a name read from a folder) |

Each row has a test on its text in `tests/unit/test_cli.py`,
`tests/unit/test_config.py` or `tests/unit/test_demo.py`.
