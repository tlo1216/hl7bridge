# Design principles

Seven rules the code follows. Each one is enforced by a test or a linter,
named below, so a change that breaks one fails the build.

## 1. Fail loudly

A degraded path must never look healthy. Bytes outside MLLP framing are
quarantined and counted, not dropped. FHIR sync rows that cannot be mapped
are skipped with a reason and make the command exit non-zero. There is one
blind `except` in the code base, at the listener boundary, and it logs,
quarantines and answers AR.

Enforced by: ruff `BLE` (no blind excepts), `tests/unit/test_mllp.py`,
`tests/unit/test_pipeline.py`.

## 2. One explicit ACK per message

Every inbound message gets exactly one AA, AE or AR, from a decision table
that lives in `ingest/pipeline.py` and is tested row by row. Duplicates are
re-acknowledged without reprocessing.

Enforced by: `tests/unit/test_pipeline.py`, `tests/system/test_mllp_end_to_end.py`.

## 3. No PHI in logs or error text

Log records carry store row ids, hospital ids, message types from
configuration, counts and reason codes. They never carry names, MRNs, birth
dates, accessions, study UIDs, report text or the control id, not even as a
hash: a hash of a short identifier is reversed by trying every candidate.
Error messages name a status, a path and a fixed reason, never a field value
or a server body, and they are raised without a chained cause that could
quote one. The CLI prints one sanitized line per error. HTTP client records
are rebuilt from an allowlist of safe fields, URL queries and user info are
stripped from every other record, and pydicom's own validation messages are
captured and replaced. ACKs name a reason code and a field, never the value.

Reports printed to stdout are the product and do show identifiers such as
accessions and MRNs.

Enforced by: `tests/unit/test_phi_logging.py`, which captures the root
logger at DEBUG and scans for canary values on every path.

## 4. UTC inside, aware timestamps only

HL7 timestamps get the hospital's configured time zone unless they carry
an explicit offset. Everything is stored as ISO 8601 UTC. A naive datetime
is refused at every gate.

Enforced by: ruff `DTZ`, `tests/unit/test_hl7time.py`, `tests/unit/test_store.py`.

## 5. Field positions live in configuration

Where a hospital puts its accession, which message types it sends, its
time zone and its lint profile are per-hospital YAML in `config/hospitals/`.
Code addresses fields by HL7 position strings such as `"OBR-3"`.

Enforced by: `tests/unit/test_config.py`.

## 6. System tests against ground truth

The synthetic generator writes a manifest of every seeded defect and the
expected outcome while it seeds it. System tests compare results against
that manifest instead of re-deriving the answer.

Enforced by: `tests/system/test_mllp_end_to_end.py`,
`tests/system/test_lint_corpus.py`, `tests/unit/test_manifest_verifier.py`.

## 7. A few high-precision lint rules

Each lint rule has a test that proves it fires on a bad message and stays
silent on a good one. Over the whole generated corpus the findings must equal
the seeded defects as an exact set, so a new false positive fails the build.

Enforced by: `tests/unit/test_lint.py`, `tests/system/test_lint_corpus.py`.
