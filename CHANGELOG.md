# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/). Until 1.0.0 the CLI and the
profile format may still change between minor versions.

## [0.2.0] - Unreleased

### Security

- Patient identifiers no longer reach logs or error text. The CLI holds the
  `httpx` and `httpcore` loggers at WARNING, and a filter strips the query
  string from every URL in their records, so the MRN or accession in a FHIR
  conditional-update URL is never printed, even at DEBUG.
- Pipeline log lines carry the store row id and a 12-digit hash of MSH-10
  instead of MSH-10 itself. Sender-written message types outside the
  hospital profile are not logged.
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
  folder only if the folder holds the marker file the script wrote.

### Added

- `hl7bridge --version`.
- A clear error when `serve` cannot bind its port.
- `tools/check_references.py`, run in CI, and `docs/principles.md`,
  `docs/testing.md`.
- `LICENSE` (Apache-2.0), `NOTICE`, `CONTRIBUTING.md`, `SECURITY.md`, issue
  and pull request templates.
- CI on Ubuntu, Windows and macOS.

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
