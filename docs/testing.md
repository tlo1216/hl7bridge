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
| `test_generate_radiology.py` | `generate-radiology` on hand-written Synthea-style Bundles (`tests/fixtures/synthea`): every message parses, lints clean, maps to FHIR and gets AA from the store listener and the direct listener (patients untouched); same bytes for the same seed, keys that never move with the seed, no other patient's messages changed by adding one; no socket opened; each usage error and each skip reason |
| `test_manifest_verifier.py` | The manifest matches the generated files, plus tamper tests |
| `test_loaders.py` | DICOM and outbound-log loaders fail loudly on bad input |
| `test_reconcile.py` | Reconciliation, including the clean-fixture zero-flag guard |
| `test_acks.py` | Report delivery tracking outcomes |
| `test_tat.py` | Turnaround-time statistics against hand-computed values, DST intervals, full precision |
| `test_lint.py` | Every lint rule fires on a bad message and stays silent on a good one; path keys and line endings |
| `test_fhir_mapper.py`, `test_fhir_client.py` | Store-row FHIR mapping (shared code tables, profile identifier systems, Encounter) and the conditional-update client against a mock transport |
| `test_v2fhir.py` | Message to FHIR: every sample against its golden Bundle in `tests/golden/to-fhir`; every refusal names the field and never the value (a canary is planted in each refused field); the search-key check on PID-3, PV1-19, the accession and every result key part (`,` `|` `\` `$` and control characters, escaped or not); the result key (`<accession>/<OBX-3.1>/sub/<OBX-4>` or `.../set/<OBX-1>`, never colliding) and its refusals; PID-3 picking the MR repetition; times in the MSH-7 offset, dates kept as dates and refused for instants; text read whole and malformed escapes refused; narrative OBX kept out of Observations; decimals written with their own digits; every OBX-2 value type; each code table against the IG maps; Encounter from PV1 and A08 updating the same key; and the spec drift checks: `docs/mapping.md` equals the spec, no mapper reads a field the spec does not list, every spec row is read by a fixture, and no mapper module reads the message except through the field reader |
| `test_fhir_sync_mapping.py` | Against an in-memory FHIR server (`tests/fakefhir.py`): `fhir sync` rebuilds resources from the stored messages (merged patients' messages too, A40 chains edge by edge) and skips (and reports) messages with a code not in its table; `fhir sync` and `to-fhir --post` write identical resources, element for element; ADT^A40 (existing links kept, contradicting and reversed merges refused, one resource twice refused, a lost create a retryable conflict); the merge (a thin ORU or A08 erases nothing, a cancel keeps the order time, a stale version fails, two matches are refused); a correction retires only its own dropped results; the run-order check; decimals in the posted bytes; the transaction check accepts only a `transaction-response` with one entry per request, each 200 or 201 |
| `test_cli_messages.py` | `explain` against golden files in `tests/golden/explain`, escapes, control characters and refusals; `send` from stdin, `--repeat` (same bytes, one stored row), `--rate` (pauses between messages only), `--expect`, the loopback warning; `send` splitting a batch file into one frame per message; `replay` (dry run, read-only store, exact bytes after a profile fix, duplicates on the same store, the same bytes quarantined twice sent once, a version 2 store, control characters stripped, filters, usage errors, never creating a database); `to-fhir` (stdout, `--out` never overwriting, batch files, decimals as sent, strict UTF-8, `--post` in order and stopping at the first Bundle not committed, nothing posted when one message fails unless `--skip-failures`, an older report after a newer one refused, a named server required) |
| `test_phi_logging.py` | No PHI in logs or error text, on every path (below) |
| `test_cli.py` | CLI behavior: grouped help and examples for every command, the 0.3 names gone, defaults for profiles and database, exit codes, the exact text of every error message in `docs/cli.md`, the listener's bind errors and banner, `send` (AE details, LF note, no ACK in time, refused), `--json`, `init`, `doctor`, `profile show` and `check` |
| `test_demo.py` | `hl7bridge demo` end to end, where it writes, and `remove_owned`, the only code that deletes: a named `--data-dir` is never deleted from and a non-empty one is refused; files and folders are created exclusively; a foreign file, a replaced file, a hard link to another file, a replaced folder, a folder or file swapped for a symlink or junction (before the run and right before the step that would delete through it), and a root swapped for another folder or a link are all left alone; the listener binds 127.0.0.1; `HL7BRIDGE_FHIR_BASE` never makes the demo write to FHIR; credentials in `--fhir-base` are never printed |
| `test_packaging_files.py` | Bundled samples get the expected ACKs and lint findings; every profile key is in `docs/profiles.md`; Dockerfile pins, user, health check and labels; compose services, volumes and loopback ports; `tools/check_ports.py` and `tools/check_licenses.py` on good and bad input |
| `test_check_references.py` | The reference check's matching works on stand-in words and the tool does not match itself; with `HL7BRIDGE_REFCHECK_SAMPLES` set, every real rule catches a local sample |
| `test_direct.py`, `test_direct_tools.py`, `test_direct_safety.py` | Against an in-memory FHIR server: the direct listener's ACK table and failure boundaries, patient matching, run ownership, identity and locks; `fhir reset`, `from-fhir`, mapping hooks; and one test per safety rule (ACK escaping, the log bound to the server and run, patient and authority checks, the deadline and worker pool, failures after the commit, reset guards, from-fhir output, hook guards), each failing when its rule is removed |

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
a mock transport (creates, updates and skipped rows, including a row skipped
for a code outside its table), each FHIR failure, `fhir probe`, `to-fhir`
over the whole corpus and each `--post` failure, `explain` (values on stdout
only), `replay --dry-run`, and the CLI through `main()` with the real
logging setup and error boundary. Error text is checked as the full formatted traceback, so a
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
| `test_to_fhir_hapi.py` | Against a live HAPI server, under identifier systems made fresh for the run: every sample Bundle posted twice; every entry committed, the second round all 200 with counts unchanged; the structured report links its two Observations; the A08 left the Encounter finished |
| `test_post_merge_hapi.py` | Against a live HAPI server, under fresh systems: a thin ORU leaves the Patient as the ADT wrote it; an A08 without PV1-44 keeps the admit time and a later one keeps the discharge; a correction sets the result it dropped to `entered-in-error`; a stale `If-Match` fails the whole transaction; `fhir sync` after `to-fhir --post` of the same messages changes no resource; six ADT^A40 tests: the merged-away Patient keeps its data and links and gets `replaced-by`, a bare inactive Patient when the server has none, a chain and a contradicting or reversed merge refused, two MRNs on one server Patient refused, a double write to one id answered `returned 400` as the fake does, and `fhir sync` of a stored chain equal to a direct post. A failing test prints HAPI's OperationOutcome issues (`tests/system/conftest.py`) |
| `test_direct_hapi.py` | Against a live HAPI server, the direct listener's failure boundaries: A04, ORM and ORU commit in the run and the Patient keeps version 1; A08 gets AR; a committed transaction whose answer is lost or times out gets AE and the resend changes no count; the same message on two MLLP connections, and two control ids for one report on two threads, leave one set; a late older report is `stale_result`; two Patients with one MRN are `ambiguous_patient`; two runs keep their own copies; a key match without the run tag is `outside_run` and left alone; `fhir reset` removes one run only, a refused entry deletes nothing, and a resource retagged after the plan is kept; a resend after a reset is written again |
| `test_readme.py` | The README blocks marked `readme-test` run as written in a fresh folder, and every `hl7bridge` command in the README and `docs/cli.md` parses |

The HAPI tests skip unless `HL7BRIDGE_FHIR_BASE` points at a server. CI
runs them against HAPI 8.12.0-2 and 8.8.0-1, each a service container, and sets `HL7BRIDGE_FHIR_REQUIRED=1`, so
a missing server fails the job instead of skipping.

## Golden files

`tests/golden/to-fhir` holds the Bundle for each bundled sample that maps,
and `tests/golden/explain` the `explain` text for each sample. After an
intended change, run the two tests once with `HL7BRIDGE_UPDATE_GOLDEN=1`
and read the diff before committing it.

## The wheel install job

The `pip-path` CI job builds the wheel with build tools from a hashed lock
file, then installs it with plain pip, so the runtime dependencies resolve
the way a pip user gets them, unhashed. That is on purpose: it checks that
the version ranges in `pyproject.toml` still install and work, which the
locked jobs cannot see. The job holds no secrets and a read-only token. A
hashed requirements file for people who pin a release belongs with the
PyPI release.
