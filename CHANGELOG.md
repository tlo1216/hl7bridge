# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/). Until 1.0.0 the CLI and the
profile format may still change between minor versions.

## [0.3.0] - Unreleased

### Breaking changes

- Commands are renamed and grouped: `serve` is `listen`, `fhir-sync` is
  `fhir sync`, `fhir-probe` is `fhir probe`, `load-studies` is
  `load studies`, `load-outbound` is `load outbound`. The old names still
  work in 0.3.x with a warning and go away in 0.4.0.
- `lint` and `send` take the file or folder as an argument
  (`hl7bridge lint msgs`). `--dir` is still accepted.
- The profiles moved from `config/hospitals/` to `src/hl7bridge/profiles/`
  and ship inside the wheel. `--config` is no longer required: the lookup
  is `--config`, `HL7BRIDGE_CONFIG`, `./hl7bridge/profiles`, then the
  bundled profiles. Scripts that passed `--config config/hospitals` must
  drop the option or point at the new folder.
- `--db` is no longer required: `--db`, `HL7BRIDGE_DB`, then
  `./hl7bridge.db`. `listen` and `load` create the database; `summary`,
  `reconcile`, `ack-report`, `tat` and `fhir sync` open an existing one
  only and exit 2 if there is none, so a report run in the wrong folder no
  longer leaves an empty database behind. A SQLite file holding another
  program's tables is refused instead of getting hl7bridge's tables added.
- `fhir sync` needs a server named with `--fhir-base` or
  `HL7BRIDGE_FHIR_BASE`; it no longer falls back to
  `http://localhost:8080/fhir`. `fhir probe` and `doctor` still do, and send
  no credentials to that fallback.
- A `--fhir-base` or `HL7BRIDGE_FHIR_BASE` that is not an http or https URL
  with a host and no query exits 2, without echoing the value.
- Exit codes are fixed and documented in `docs/cli.md`: 0 success, 1
  findings or rejected messages, 2 usage or configuration error, 3
  connection error, 4 internal error, 130 Ctrl+C. Before, a configuration
  error exited 1 and an internal error 2.
- `send` exits 1 when any message gets AE or AR, and 3 when no ACK comes
  in time or the listener does not answer. It exited 0 before.
- `fhir sync` and `fhir probe` exit 3 when the server never answered.
- Error lines start with `error:` instead of `hl7bridge: error:`, and end
  with a hint. Errors whose text could quote input no longer print stack
  frames unless `--debug` is given.
- Log output is quieter by default: WARNING for every command except
  `listen`, which logs at INFO. `-v` and `-vv` bring INFO and DEBUG back.
- A database with another schema version, or a file that is not a
  database, raises a clear error instead of a `RuntimeError`.
- `scripts/demo.sh` is deprecated and goes away in 0.4.0. It now only runs
  `hl7bridge demo` with the arguments given: no HAPI container, and no
  deleting. `scripts/demo_guard.sh` is removed. Use the compose demo for
  HAPI in containers.
- `demo --data-dir` takes only a new or empty folder, writes there and
  never deletes anything in it. Reusing an earlier demo folder, and the
  `.hl7bridge-demo` marker that allowed it, are gone.
- `tat --month` must be a real month (`YYYY-MM`, 01 to 12), and missing
  input files (`load outbound --path`, `load studies --dicom`), an output
  path that is a file (`synth --out`, `init`), and file system errors
  exit 2 with a message, not 4 ("a bug in hl7bridge").
- The demo exits 1 when the FHIR step skipped a row, and an HTTP error
  from the FHIR server's `/metadata` exits 1 (3 is only for no answer).

### Added

- `hl7bridge demo`: the whole pipeline in one process on every OS, with a
  listener on a free loopback port, a check of every count against the
  manifest, and an optional FHIR step against a server named with
  `--fhir-base` (`--fhir-wait` polls a server that is still starting). By
  default the demo writes into a new temporary folder and removes it after
  a clean run, unless `--keep`. It removes only what it created in that
  run: each file and folder is created exclusively and recorded with its
  file identity, and is deleted only while it still has that identity,
  never through a link (on Windows through the handle that was checked). Anything else is left and the folder is kept.
  `hl7bridge.demofiles` is the only code in hl7bridge that deletes.
- `hl7bridge init [FOLDER]`: copies the bundled profiles and twelve
  hand-made sample messages (one per type and dialect, two broken). Never
  overwrites without `--force`.
- `hl7bridge doctor`: Python, version, profiles, time zones, database
  (read-only), listen port and FHIR server, each with a fix.
- `hl7bridge profile show` and `profile check`, and `docs/profiles.md`
  with every key, its type, default and purpose.
- `--json` on `reconcile`, `ack-report`, `tat`, `summary`, `lint`,
  `doctor` and the `profile` commands.
- `-v`, `-q`, `--debug`, accepted before or after the command; grouped
  `--help` and examples for every command; every command says which
  profile folder and database it used.
- `HL7BRIDGE_FHIR_TOKEN` (bearer token) and `HL7BRIDGE_FHIR_BASE` as the
  default FHIR server.
- Clear messages for a taken port, a port that needs admin rights, an
  address not on this machine, a FHIR server that does not answer, no
  `.hl7` files, LF line ends (converted, with a note), AE and AR ACKs
  (code in words, MSA-3, ERR, quarantine reason, next command), no ACK in
  time, a refused connection, and an old database schema.
- `Dockerfile` (multi-stage, wheel built with uv, dependencies installed
  from `uv.lock` with hashes, non-root user, health check, base images
  pinned by digest) and `compose.demo.yaml` (HAPI with no host port, the
  listener on 127.0.0.1 only, the demo against HAPI, named volumes), plus
  `compose.hapi-port.yaml` to browse HAPI on 127.0.0.1.
- `tools/check_ports.py`: fails on any published port that is not bound
  to 127.0.0.1, in compose files (found by content, long and short port
  forms), workflow service and job containers, and `docker run`,
  `docker create`, `podman run` and `docker compose run` lines, including
  clustered flags such as `-dp`, host networking and publish-all.
- `tools/check_licenses.py`: checks every runtime dependency's license
  against an allow list. Without a `License-Expression`, the `License`
  field and every license classifier must all be allowed; an unknown one
  fails. CI runs it on every OS in the test matrix.
- `docs/cli.md`: commands, defaults, exit codes and error messages.

### Changed

- CI runs once per change: pushes to `main` and pull requests, not both.
- CI pins uv by version, pip, build and hatchling by hash, and the HAPI
  image by digest. The wheel job no longer checks a hard-coded file count;
  it compares lint's JSON with the files the generator wrote, and runs
  `init`, `doctor` and `demo` from the installed wheel.
- New CI jobs: license and port checks, the reference check on commit
  messages, the README quickstart, and a Docker job that builds the image,
  checks the user is not root and runs the compose demo.
- Every CI job has a timeout (15 to 30 minutes), and the HAPI service
  container in CI is published on 127.0.0.1 only.
- The image builds its wheel with hatchling from the hashed build lock
  (`uv build --build-constraints ... --require-hashes`).
- The compose demo no longer keeps its data in a volume; it uses a
  temporary folder in its container, so `up` can run again and again.

### Fixed

- `listen` prints its banner only after the socket is bound, names the
  real cause of a bind failure, and no longer labels a later error as a
  bind failure.
- `lint` reports a file that is not UTF-8 as `HL7-001` instead of
  stopping with an internal error.

### Security

- User names and passwords in a FHIR URL are never printed, including in a
  mistyped URL where the parser puts them in the path.
- `HL7BRIDGE_FHIR_APIKEY` and `HL7BRIDGE_FHIR_TOKEN` go only to a server
  you named, with a warning when they would go over plain http to another
  machine.
- Text a remote party wrote (MSA-3 and ERR in `send`, the ACK reason in
  `ack-report`, the server software name in `fhir probe`) is printed with
  control characters replaced, so a listener cannot move the cursor, set
  the terminal title or hide lines. `send` shows a quarantine reason only
  for hl7bridge's own reason codes.
- `--json` output lists every field by name for each report, so a field
  added to a report later does not reach JSON by accident.

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
