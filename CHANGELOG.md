# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/). Until 1.0.0 the CLI and the
profile format may still change between minor versions.

## [0.2.0] - Unreleased

### Security

- Patient identifiers no longer reach logs or error text. The CLI holds the
  `httpx`, `httpcore` and `pydicom` loggers at WARNING. HTTP client records,
  from `httpx`, `httpcore` and every logger below them, are rebuilt from a
  safe field list (method, URL with no query or user info, HTTP version,
  status, trace event name), so reason phrases and headers such as
  `Location` are never printed, even at DEBUG. The filter sits on those
  loggers and on the CLI's handler. Every other record loses URL queries
  and user info, its traceback included.
- Pipeline log lines carry the store row id instead of MSH-10. MSH-10 is
  not logged in any form. Sender-written message types outside the
  hospital profile are not logged.
- The CLI has one error boundary: each failure prints a single line, with
  no chained cause. Errors whose text can quote input print the class name
  and stack frames only. Loader and FHIR client errors are raised without
  a chained cause.
- pydicom's validation messages (logging and `warnings`) are captured while
  a DICOM file is read and turned into a loader error that names the
  hospital folder and file position, not the value or the file name.
- ACKs for rejected messages carry the reason code and the field name in
  MSA-3, never the rejected value. The MSH-9 trigger is echoed only when
  well formed.
- OperationOutcome issue codes outside the FHIR IssueType value set print
  as `unrecognized-code`.
- User names and passwords in a `--fhir-base` URL are never printed.
- `load-outbound` now takes `--config` and rejects lines that name an
  unconfigured hospital. `ack-report` counts outbound reports for a
  hospital no longer configured in an `UNCONFIGURED HOSPITAL` line instead
  of failing with the control id in the message.
- The listener answers AR even when the quarantine write fails too, and
  logs only type names for both failures.
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
  folder only if the folder holds the marker file the script wrote. It
  creates that folder with a plain `mkdir` before marking it, refuses `..`
  components and symlinks with trailing slashes, compares protected folders
  by file identity, and stops if the MLLP port is taken or an unknown FHIR
  server answers at the default address.

### Added

- `hl7bridge --version`.
- A clear error when `serve` cannot bind its port.
- `tools/check_references.py`, run in CI, and `docs/principles.md`,
  `docs/testing.md`.
- `LICENSE` (Apache-2.0), `NOTICE`, `CONTRIBUTING.md`, `SECURITY.md`, issue
  and pull request templates.
- CI: checks on Ubuntu; tests on Ubuntu and Windows for every push and
  pull request, plus macOS for pushes to `main` and manual runs; the demo
  guard tests may not skip in CI.

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
