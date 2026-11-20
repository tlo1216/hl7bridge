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
| `test_cli.py` | CLI behavior: `--version`, a clear message when the port is taken |
| `test_demo_guard.py` | `scripts/demo.sh` deletes only a folder it created (needs bash; skips without it) |
| `test_check_references.py` | The reference check catches every pattern and does not match itself |

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
- loader input that fails validation.

It runs ingest over the whole corpus, both loaders, a full FHIR sync through
a mock transport (creates, updates and skipped rows), each FHIR failure,
`fhir-probe`, and `hl7bridge fhir-sync` through `main()` with the real
logging setup. Each test also asserts that logging happened, so none can
pass by being silent.

## System tests (`tests/system/`)

| File | What it proves |
| --- | --- |
| `test_mllp_end_to_end.py` | The generated corpus over real sockets matches the manifest, TAT outcomes included |
| `test_lint_corpus.py` | Lint findings over the whole corpus equal the seeded defects as an exact set |
| `test_fhir_hapi.py` | Against a live HAPI server: post everything twice, prove idempotency and corrected-report update in place |

The HAPI tests skip unless `HL7BRIDGE_FHIR_BASE` points at a server. CI
starts HAPI as a service container and sets `HL7BRIDGE_FHIR_REQUIRED=1`, so
a missing server fails the job instead of skipping.
