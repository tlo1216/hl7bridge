# Tests

Run everything that needs no outside service:

```
uv run pytest -m "not fhir"
```

or, without uv, `python -m pytest -m "not fhir"` inside the virtual
environment.

## Unit tests (`tests/unit/`)

| File | What it proves |
| --- | --- |
| `test_hl7time.py` | Timestamp parsing and formatting, DST boundaries included; naive values are refused |
| `test_config.py` | Hospital profiles are strict: unknown keys, bad field references, bad time zones and duplicate facility codes fail |
| `test_parser.py` | Spec-position field addressing, a single unescape pass, loud parse failures |
| `test_mllp.py` | MLLP framing round trip, split delivery, junk bytes surfaced instead of dropped, size guards |
| `test_pipeline.py` | Every row of the ACK decision table, against a real store |
| `test_store.py` | Store invariants: duplicates, report status ranking, UTC-only timestamps |
| `test_synth.py` | Generator determinism, parseable output, no OS newline translation |
| `test_manifest_verifier.py` | The manifest matches the generated files, plus tamper tests |
| `test_loaders.py` | DICOM and outbound-log loaders fail loudly on bad input |
| `test_reconcile.py` | Reconciliation, including the clean-fixture zero-flag guard |
| `test_acks.py` | Report delivery tracking outcomes |
| `test_tat.py` | Turnaround-time statistics against hand-computed values, DST intervals, full precision |
| `test_lint.py` | Every lint rule fires on a bad message and stays silent on a good one; path keys and line endings |
| `test_fhir_mapper.py`, `test_fhir_client.py` | FHIR mapping and the conditional-update client against a mock transport |
| `test_phi_logging.py` | No PHI in logs or error text, on every path (below) |
| `test_cli.py` | CLI behavior: grouped help and examples for every command, old names, defaults for profiles and database, exit codes, the exact text of every error message in `docs/cli.md`, the listener's bind errors and banner, `send` (AE details, LF note, no ACK in time, refused), `--json`, `init`, `doctor`, `profile show` and `check` |
| `test_demo.py` | `hl7bridge demo` end to end, and `remove_demo_dir`, the only code that deletes: no marker, symlinks and junctions, home, current folder and parent, repository and drive roots, `..`, foreign files, a folder swapped for a link between the check and the delete, another device, foreign markers, marker entries that try to escape |
| `test_packaging_files.py` | Bundled samples get the expected ACKs and lint findings; every profile key is in `docs/profiles.md`; Dockerfile pins, user, health check and labels; compose services, volumes and loopback ports; `tools/check_ports.py` and `tools/check_licenses.py` on good and bad input |
| `test_demo_guard.py` | `scripts/demo.sh` deletes only a folder it created: `..` paths, symlinks and junctions with or without trailing slashes, other spellings of protected folders, spaces, a marker written by `hl7bridge demo`; and its listener stop ends the whole process tree on Windows (needs bash; skips without it unless `HL7BRIDGE_REQUIRE_BASH=1`, which CI sets) |
| `test_check_references.py` | The reference check's matching works on stand-in words and the tool does not match itself; with `HL7BRIDGE_REFCHECK_SAMPLES` set, every real rule catches a local sample |

### The PHI gate

`test_phi_logging.py` captures the root logger at DEBUG, so the HTTP client's
own loggers are included. It scans the captured text, and its URL-decoded
form, for every name, MRN, birth date, accession, study UID, control id and
report phrase in the synthetic store, plus hand-made canaries planted in:

- MSH-10 and MSH-9 of accepted, duplicate and rejected messages,
- a broken segment, a non-UTF-8 payload and a parser whose error quotes its input,
- an exception message at the listener boundary,
- FHIR server bodies that echo the request (400 OperationOutcome, 412),
  transport errors and timeouts,
- the reason phrase, `Location` header and other headers of a real HTTP
  server on 127.0.0.1, so httpcore's own DEBUG records are exercised,
- invalid DICOM values (a bad UI and a bad TM), which pydicom reports
  through both logging and `warnings`,
- loader input that fails validation, and outbound log lines naming an
  unknown hospital,
- ACK text for rejected messages, and a listener boundary whose quarantine
  write fails too.

It runs ingest over the whole corpus, both loaders, a full FHIR sync through
a mock transport (creates, updates and skipped rows), each FHIR failure,
`fhir probe`, and the CLI through `main()` with the real logging setup and
error boundary. Error text is checked as the full formatted traceback, so a
chained cause cannot hide a value. Each test also asserts that logging
happened, so none can pass by being silent. An autouse fixture removes the
hl7bridge filters from the HTTP client loggers before each test, so no test
passes on a filter an earlier test left behind.

## System tests (`tests/system/`)

| File | What it proves |
| --- | --- |
| `test_mllp_end_to_end.py` | The generated corpus over real sockets matches the manifest, TAT outcomes included |
| `test_lint_corpus.py` | Lint findings over the whole corpus equal the seeded defects as an exact set |
| `test_fhir_hapi.py` | Against a live HAPI server: post everything twice, prove idempotency and corrected-report update in place |
| `test_readme.py` | The README blocks marked `readme-test` run as written in a fresh folder, and every `hl7bridge` command in the README and `docs/cli.md` parses |

The HAPI tests skip unless `HL7BRIDGE_FHIR_BASE` points at a server. CI
starts HAPI as a service container and sets `HL7BRIDGE_FHIR_REQUIRED=1`, so
a missing server fails the job instead of skipping.
