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
| FHIR server | `--fhir-base`, `HL7BRIDGE_FHIR_BASE`, `http://localhost:8080/fhir` (`demo` uses only `--fhir-base`) |
| FHIR credentials | `HL7BRIDGE_FHIR_APIKEY` (an `apikey` header), `HL7BRIDGE_FHIR_TOKEN` (a bearer token), from the environment only |
| Listener | `--host 127.0.0.1 --port 2575` |

Every command that reads profiles or the store prints which folder and
database it used, on stderr.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Findings, rejected messages (AE or AR) or a failed check |
| 2 | Usage or configuration error: bad arguments, a bad profile, a path or database that cannot be used, a port that cannot be bound |
| 3 | Connection error: a listener or server did not answer, or no ACK came in time |
| 4 | Internal error: a bug in hl7bridge. Run again with `--debug` and report it |
| 130 | Interrupted with Ctrl+C |

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
| ACK is AE or AR | On stdout: the code in words, MSA-3, ERR if present, the quarantine reason, and `next: hl7bridge lint <file>` |
| No ACK in time | `error: no ACK from 127.0.0.1:2575 within 10 s. The message may or may not have been processed.` (exit 3) |
| Listener not running | `error: cannot connect to 127.0.0.1:2575: connection refused. Is the listener running? Try: hl7bridge listen` |
| Old database schema | `error: database hl7bridge.db has schema version 1; this hl7bridge needs version 2. There is no migration. Demo databases can be deleted and made again.` |
| Not a database | `error: notes.txt is not an hl7bridge database (DatabaseError). Pass --db with another path, or delete the file if it is a demo database.` |
| Demo folder not owned | `error: refusing to use data: it has no .hl7bridge-demo marker, so the demo did not create it. Remove it yourself, or pass --data-dir with a new folder.` |

Each row has a test on its text in `tests/unit/test_cli.py`,
`tests/unit/test_config.py` or `tests/unit/test_demo.py`.
