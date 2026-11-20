"""hl7bridge command-line interface.

The parser is built from one table of commands, so the grouped help, the
examples and the deprecated names stay in step. ``main`` is the error
boundary: a user error prints one ``error:`` line with a hint and never a
traceback, and nothing it prints can quote a value read from a message.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import os
import re
import socket
import sqlite3
import sys
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import __version__
from .acks import render_ack_text, track_acks
from .acks.tracker import STATES
from .config import HospitalRegistry, check_profile_dir, profile_as_dict
from .demo import DemoOptions, run_demo
from .doctor import FAIL, run_checks
from .errors import (
    EXIT_CONNECTION,
    EXIT_FINDINGS,
    EXIT_INTERNAL,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    BridgeError,
    FhirPostError,
    ListenError,
    NetworkError,
    UsageError,
)
from .fhir import HapiClient, render_sync_text, sync_to_fhir
from .ingest import IngestPipeline, send_messages, serve
from .ingest.ack import ALL_REASONS
from .ingest.loaders import load_outbound, load_studies
from .ingest.parser import get_field, parse_hl7
from .jsonout import dumps, to_jsonable
from .lint import lint_directory, render_lint_text
from .logsafety import configure_logging, printable, safe_url
from .msgfiles import display_key, hl7_files
from .profiles import Resolved, resolve_db, resolve_profiles
from .reconcile import reconcile, render_reconcile_text
from .scaffold import DEFAULT_FOLDER, init_folder
from .store import connect, message_counts, quarantine_summary
from .synth import generate, verify_manifest
from .tat import render_tat_text, tat_report

log = logging.getLogger(__name__)

DEFAULT_FHIR_BASE = "http://localhost:8080/fhir"
FHIR_BASE_ENV = "HL7BRIDGE_FHIR_BASE"
FHIR_APIKEY_ENV = "HL7BRIDGE_FHIR_APIKEY"
FHIR_TOKEN_ENV = "HL7BRIDGE_FHIR_TOKEN"
REMOVED_IN = "0.4.0"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

ACK_CODE_WORDS = {
    "AA": "accepted",
    "AE": "application error, the sender should fix and resend",
    "AR": "application reject, the receiver will not take this message",
    "CA": "commit accept",
    "CE": "commit error",
    "CR": "commit reject",
}


# -- small helpers ---------------------------------------------------------------


def _quiet(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "quiet", False))


def _note(args: argparse.Namespace, text: str) -> None:
    """A status line on stderr, so stdout stays clean for reports and JSON."""
    if not _quiet(args):
        print(text, file=sys.stderr)


def _profiles(args: argparse.Namespace) -> Resolved:
    resolved = resolve_profiles(getattr(args, "config", None))
    _note(args, f"using profiles: {resolved.describe()}")
    return resolved


def _registry(args: argparse.Namespace) -> HospitalRegistry:
    return HospitalRegistry.load_dir(_profiles(args).path)


def _db_path(args: argparse.Namespace) -> Path:
    resolved = resolve_db(getattr(args, "db", None))
    _note(args, f"using database: {resolved.describe()}")
    return resolved.path


def _now_arg(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise UsageError(f"--now {value!r} is not an ISO 8601 timestamp.") from None
    if dt.tzinfo is None:
        raise UsageError(
            f"--now {value!r} has no UTC offset.",
            hint="Try --now 2026-10-08T12:00:00+00:00",
        )
    return dt


def _month(value: str) -> str:
    """YYYY-MM with a real month, for ``tat --month``."""
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if match is None or not 1 <= int(match.group(2)) <= 12:
        raise argparse.ArgumentTypeError(f"{value!r} is not a month in the form YYYY-MM")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port number") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{port} is not between 0 and 65535")
    return port


def _fhir_headers() -> dict[str, str]:
    """Auth headers from the environment, never from argv (argv shows in
    process listings). HL7BRIDGE_FHIR_APIKEY feeds an ``apikey`` header,
    HL7BRIDGE_FHIR_TOKEN a bearer token."""
    headers: dict[str, str] = {}
    api_key = os.environ.get(FHIR_APIKEY_ENV)
    if api_key:
        headers["apikey"] = api_key
    token = os.environ.get(FHIR_TOKEN_ENV)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def check_fhir_base(value: str, source: str) -> str:
    """``value`` if it is a usable FHIR base URL, else UsageError.

    The scheme must be http or https and a host must be present, with no
    query or fragment. The error names where the value came from and never
    echoes it: a mistyped URL can hold a password in a place no parser
    recognizes as user info."""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018  raises ValueError for a port that is not a number
    except ValueError:
        host = None
        parts = None
    if (
        parts is None
        or parts.scheme not in ("http", "https")
        or not host
        or parts.query
        or parts.fragment
        or "@" in parts.path
    ):
        raise UsageError(
            f"{source} is not an http(s) URL with a host (value not shown).",
            hint="Try: --fhir-base http://localhost:8080/fhir",
        )
    return value


def _fhir_base(args: argparse.Namespace) -> tuple[str, bool]:
    """(base URL, whether the user named it) from flag, environment, default.
    A named value is checked before anything uses or prints it."""
    if getattr(args, "fhir_base", None):
        return check_fhir_base(str(args.fhir_base), "--fhir-base"), True
    env = os.environ.get(FHIR_BASE_ENV)
    if env:
        return check_fhir_base(env, FHIR_BASE_ENV), True
    return DEFAULT_FHIR_BASE, False


def _warn_plain_http(base: str, headers: dict[str, str]) -> None:
    """Credentials over plain http to another machine can be read on the way."""
    parts = urlsplit(base)
    has_credentials = bool(headers) or parts.username is not None
    if has_credentials and parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        print(
            f"warning: sending credentials to {safe_url(base)} over plain http;"
            " anyone on the network path can read them. Use https.",
            file=sys.stderr,
        )


def _require_metadata(client: HapiClient, base: str) -> None:
    status = client.metadata_status()
    if status == 200:
        return
    if status == 0:
        raise NetworkError(
            f"no answer from {safe_url(base)}/metadata. Is the server running?",
            hint="Try: hl7bridge doctor",
        )
    err = FhirPostError(status, f"{safe_url(base)}/metadata", reason="the server is not ready")
    if status in (401, 403):
        err.hint = f"Set {FHIR_APIKEY_ENV} or {FHIR_TOKEN_ENV} if the server needs one."
    raise err


def _print_json(value: object) -> None:
    print(dumps(value))


# -- Try it ------------------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    profiles = _profiles(args)
    opts = DemoOptions(
        profiles=profiles.path,
        data_dir=Path(args.data_dir) if args.data_dir else None,
        keep=args.keep,
        seed=args.seed,
        # Only the flag, never HL7BRIDGE_FHIR_BASE: the demo writes into a
        # server only when it is named on this command line.
        fhir_base=check_fhir_base(args.fhir_base, "--fhir-base") if args.fhir_base else None,
        fhir_wait_s=args.fhir_wait,
        fhir_headers=_fhir_headers() if args.fhir_base else {},
        client_factory=HapiClient,
    )
    # The pipeline logs every rejection, and the demo seeds rejections on
    # purpose; the demo prints its own account of them instead.
    package_log = logging.getLogger("hl7bridge")
    saved_level = package_log.level
    if getattr(args, "verbose", 0) == 0 and not getattr(args, "debug", False):
        package_log.setLevel(logging.ERROR)
    if opts.fhir_base:
        _warn_plain_http(opts.fhir_base, opts.fhir_headers)
    try:
        return run_demo(opts, sys.stdout, sys.stderr)
    finally:
        package_log.setLevel(saved_level)


def cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.folder)
    result = init_folder(dest, force=args.force)
    print(f"wrote {len(result.profiles)} profiles to {dest / 'profiles'}")
    print(f"wrote {len(result.samples)} sample messages to {dest / 'samples'}")
    print("next:")
    config = "" if dest == DEFAULT_FOLDER else f" --config {dest / 'profiles'}"
    print(f"  hl7bridge profile check{config}")
    print(f"  hl7bridge lint {dest / 'samples'}{config}")
    print(f"  hl7bridge demo{config}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    base, named = _fhir_base(args)
    # Credentials go only to a server the user named.
    headers = _fhir_headers() if named else {}
    if named:
        _warn_plain_http(base, headers)
    # The FHIR line says when the server does not answer; the client's own
    # warning would say it twice.
    client_log = logging.getLogger("hl7bridge.fhir.client")
    saved_level = client_log.level
    if not getattr(args, "debug", False):
        client_log.setLevel(logging.ERROR)
    try:
        checks = run_checks(
            profiles=resolve_profiles(args.config),
            db=resolve_db(args.db),
            host=args.host,
            port=args.port,
            fhir_base=base,
            fhir_named=named,
            fhir_headers=headers,
            client_factory=HapiClient,
        )
    finally:
        client_log.setLevel(saved_level)
    failed = any(c.status == FAIL for c in checks)
    if args.json:
        _print_json({"ok": not failed, "checks": checks})
    else:
        for check in checks:
            print(check.line())
        print("all checks passed" if not failed else "some checks failed; see Fix above")
    return EXIT_FINDINGS if failed else EXIT_OK


# -- Messages ------------------------------------------------------------------------


def _message_path(args: argparse.Namespace) -> Path:
    given = args.path or args.dir
    if not given:
        raise UsageError(f"{args.command}: give a file or folder of *.hl7 messages.")
    return Path(given)


def cmd_lint(args: argparse.Namespace) -> int:
    report = lint_directory(_message_path(args), _registry(args))
    if args.json:
        _print_json(
            {
                "files_checked": report.files_checked,
                "error_count": report.error_count,
                "findings": [
                    {"file": name, **_pick(f, LINT_FIELDS)}
                    for name, group in report.by_file
                    for f in group
                ],
            }
        )
    else:
        print(render_lint_text(report))
    # Linter convention: errors fail the invocation (warnings do not).
    return EXIT_FINDINGS if report.error_count else EXIT_OK


def _to_cr(payload: bytes) -> tuple[bytes, bool]:
    """Segments end with CR in HL7 v2. Convert CRLF and LF, and say so."""
    if b"\n" not in payload:
        return payload, False
    return payload.replace(b"\r\n", b"\r").replace(b"\n", b"\r"), True


@dataclass(frozen=True, slots=True)
class _AckView:
    code: str
    text: str
    err: str


_ACK_CODE = re.compile(r"[A-Z]{2}")


def _read_ack(raw: bytes) -> _AckView:
    """The ACK fields ``send`` shows. The receiver wrote them, so the code
    must look like an ACK code and the texts lose control characters."""
    message = parse_hl7(raw.decode("utf-8", errors="replace"))
    err_parts = [get_field(message, f) for f in ("ERR-3", "ERR-1")]
    code = get_field(message, "MSA-1") or ""
    return _AckView(
        code=code if _ACK_CODE.fullmatch(code) else "??",
        text=printable(get_field(message, "MSA-3") or ""),
        err=printable(next((p for p in err_parts if p), "")),
    )


def cmd_send(args: argparse.Namespace) -> int:
    root = _message_path(args)
    paths = hl7_files(root)
    payloads: list[bytes] = []
    for path in paths:
        payload, converted = _to_cr(path.read_bytes())
        if converted:
            print(
                f"note: {display_key(path, root)} uses LF line ends; converted to CR for sending."
            )
        payloads.append(payload)

    acks: list[bytes] = []
    where = f"{args.host}:{args.port}"
    try:
        asyncio.run(
            send_messages(args.host, args.port, payloads, ack_timeout_s=args.timeout, acks=acks)
        )
    except TimeoutError:
        _print_send_results(root, paths, acks)
        raise NetworkError(
            f"no ACK from {where} within {args.timeout:g} s."
            " The message may or may not have been processed."
        ) from None
    except (ConnectionRefusedError, socket.gaierror) as exc:
        reason = "connection refused" if isinstance(exc, ConnectionRefusedError) else "unknown host"
        raise NetworkError(
            f"cannot connect to {where}: {reason}. Is the listener running?",
            hint="Try: hl7bridge listen",
        ) from None
    except (ConnectionError, OSError) as exc:
        _print_send_results(root, paths, acks)
        raise NetworkError(
            f"the connection to {where} broke ({type(exc).__name__})"
            f" after {len(acks)} of {len(payloads)} ACKs."
        ) from None
    return _print_send_results(root, paths, acks)


def _print_send_results(root: Path, paths: list[Path], acks: list[bytes]) -> int:
    counts: Counter[str] = Counter()
    for path, raw in zip(paths, acks, strict=False):
        view = _read_ack(raw)
        counts[view.code] += 1
        if view.code == "AA":
            continue
        words = ACK_CODE_WORDS.get(view.code, "not a known ACK code")
        print(f"{display_key(path, root)}: {view.code} ({words})")
        if view.text:
            print(f"  MSA-3: {view.text}")
        if view.err:
            print(f"  ERR: {view.err}")
        reason = view.text.split(":", 1)[0].strip()
        if reason in ALL_REASONS:  # only hl7bridge's own reason codes
            print(f"  quarantine reason: {reason}")
        print(f"  next: hl7bridge lint {path}")
    print(f"sent {len(paths)} message(s) from {root}, got {len(acks)} ACK(s)")
    for code in sorted(counts):
        print(f"  {code}: {counts[code]}")
    rejected = sum(n for code, n in counts.items() if code != "AA")
    return EXIT_FINDINGS if rejected or len(acks) != len(paths) else EXIT_OK


def cmd_synth(args: argparse.Namespace) -> int:
    registry = _registry(args)
    out = Path(args.out)
    if out.exists() and not out.is_dir():
        raise UsageError(f"--out {out} exists and is not a folder.")
    manifest = generate(out, registry, seed=args.seed)
    print(f"generated synthetic day under {out} (seed={args.seed})")
    print(f"  hospitals: {', '.join(manifest.hospitals)}")
    print(f"  seeded defects: {len(manifest.defects)}")
    problems = verify_manifest(out, registry)
    if problems:
        for p in problems:
            print(f"  MANIFEST MISMATCH: {p}", file=sys.stderr)
        return EXIT_FINDINGS
    print("  manifest verified against generated artifacts")
    return EXIT_OK


# -- Listener --------------------------------------------------------------------------


def _listen_error(host: str, port: int, exc: OSError) -> ListenError:
    where = f"cannot listen on {host}:{port}"
    other_port = f"Try --port {port + 1}." if port else ""
    if isinstance(exc, socket.gaierror):
        return ListenError(
            f"{where}, the host name does not resolve.", hint="Try --host 127.0.0.1."
        )
    if exc.errno == errno.EADDRINUSE:
        return ListenError(f"{where}, the port is in use.", hint=other_port)
    if exc.errno == getattr(errno, "WSAEACCES", None):
        return ListenError(
            f"{where}, the port is in use by another program or reserved by Windows.",
            hint=other_port,
        )
    if exc.errno == errno.EACCES:
        return ListenError(
            f"{where}, permission denied. Ports below 1024 need admin rights.",
            hint=other_port,
        )
    if exc.errno in (errno.EADDRNOTAVAIL, getattr(errno, "WSAEADDRNOTAVAIL", None)):
        return ListenError(
            f"{where}, that address is not on this machine.", hint="Try --host 127.0.0.1."
        )
    reason = exc.strerror or type(exc).__name__
    return ListenError(f"{where}: {reason}.")


def cmd_listen(args: argparse.Namespace) -> int:
    registry = _registry(args)
    db_path = _db_path(args)
    conn = connect(db_path)
    pipeline = IngestPipeline(registry, conn)

    async def run() -> None:
        try:
            server = await serve(args.host, args.port, pipeline.handle_frame, pipeline.handle_junk)
        except OSError as exc:
            raise _listen_error(args.host, args.port, exc) from None
        bound = server.sockets[0].getsockname()[1] if server.sockets else args.port
        # Printed only once the socket is bound, so it is never a false claim.
        print(f"MLLP listener on {args.host}:{bound}, db {db_path} (Ctrl+C to stop)", flush=True)
        if args.host not in LOOPBACK_HOSTS:
            print(
                f"warning: {args.host} is not loopback. MLLP has no encryption or"
                " authentication; other machines can now send to this listener.",
                file=sys.stderr,
            )
        try:
            async with server:
                await server.serve_forever()
        except OSError as exc:
            raise NetworkError(
                f"the listener on {args.host}:{bound} stopped:"
                f" {exc.strerror or type(exc).__name__}."
            ) from None

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("listener stopped")
    finally:
        conn.close()
    return EXIT_OK


# -- Reports ---------------------------------------------------------------------------


def _report_db(args: argparse.Namespace) -> sqlite3.Connection:
    """The store for a report: an existing database only, never a new one."""
    return connect(_db_path(args), create=False)


# --json output names every field it prints. A field added to a report
# dataclass later does not appear in JSON until it is listed here.
RECONCILE_FIELDS = ("hospital_id", "display_name", "matched", "pending_orders", "pending_studies")
FINDING_FIELDS = ("category", "hospital_id", "accession", "study_uid", "detail")
OUTCOME_FIELDS = (
    "hospital_id",
    "control_id",
    "accession",
    "state",
    "sent_at_utc",
    "acked_at_utc",
    "reason",
    "duplicate_acks",
    "conflicting_codes",
)
UNKNOWN_ACK_FIELDS = ("hospital_id", "msa_control_id", "msa_code", "received_at_utc")
TAT_FIELDS = (
    "hospital_id",
    "display_name",
    "month",
    "finals",
    "prelims",
    "corrected",
    "missing_exam_time",
    "missing_final_time",
    "missing_order",
    "breaches",
)
MEASURED_FIELDS = (
    "accession",
    "modality",
    "priority",
    "exam_to_final_hours",
    "exam_to_prelim_hours",
    "order_to_exam_hours",
    "target_hours",
)
GROUP_FIELDS = ("n", "median_hours", "p90_hours", "breaches", "target_hours")
LINT_FIELDS = ("rule_id", "severity", "hospital_id", "control_id", "field", "message")


def _pick(obj: object, names: Sequence[str]) -> dict[str, object]:
    """Only the named fields of a dataclass, row or mapping, as JSON values."""
    if isinstance(obj, sqlite3.Row):
        return {n: to_jsonable(obj[n]) for n in names}
    return {n: to_jsonable(getattr(obj, n)) for n in names}


def cmd_reconcile(args: argparse.Namespace) -> int:
    now = _now_arg(args.now)
    registry = _registry(args)
    conn = _report_db(args)
    reports = reconcile(conn, registry, now)
    if args.json:
        _print_json(
            {
                hosp: {
                    **_pick(r, RECONCILE_FIELDS),
                    "finding_count": len(r.findings),
                    "findings": [_pick(f, FINDING_FIELDS) for f in r.findings],
                }
                for hosp, r in sorted(reports.items())
            }
        )
    else:
        print(render_reconcile_text(reports))
    return EXIT_OK


def cmd_ack_report(args: argparse.Namespace) -> int:
    now = _now_arg(args.now)
    registry = _registry(args)
    conn = _report_db(args)
    report = track_acks(conn, registry, now)
    if args.json:
        by_hospital = {
            hosp: {state: len(items) for state, items in report.by_state(hosp).items()}
            for hosp in sorted(registry.by_id)
        }
        _print_json(
            {
                "states": list(STATES),
                "by_hospital": by_hospital,
                "outcomes": [_pick(o, OUTCOME_FIELDS) for o in report.outcomes],
                "unknown_acks": [_pick(a, UNKNOWN_ACK_FIELDS) for a in report.unknown_acks],
                # The text report gives a count only; so does JSON.
                "unconfigured_hospital_reports": len(report.unconfigured),
            }
        )
    else:
        print(render_ack_text(report, registry))
    return EXIT_OK


def cmd_tat(args: argparse.Namespace) -> int:
    registry = _registry(args)
    conn = _report_db(args)
    reports = tat_report(conn, registry, args.month)
    if args.json:
        _print_json(
            {
                hosp: {
                    **_pick(r, TAT_FIELDS),
                    "measured": [_pick(m, MEASURED_FIELDS) for m in r.measured],
                    "groups": {
                        "/".join(key): _pick(g, GROUP_FIELDS) for key, g in r.groups.items()
                    },
                }
                for hosp, r in sorted(reports.items())
            }
        )
    else:
        print(render_tat_text(reports))
    return EXIT_OK


def cmd_summary(args: argparse.Namespace) -> int:
    conn = _report_db(args)
    counts = message_counts(conn)
    quarantined = quarantine_summary(conn)
    total = sum(quarantined.values())
    if args.json:
        _print_json({"messages": counts, "quarantine": quarantined, "quarantine_total": total})
        return EXIT_OK
    print("accepted messages by type:")
    for msg_type, n in counts.items():
        print(f"  {msg_type}: {n}")
    print(f"quarantine ({total} total):")
    for reason, n in quarantined.items():
        print(f"  {reason}: {n}")
    return EXIT_OK


# -- FHIR ------------------------------------------------------------------------------


def cmd_fhir_probe(args: argparse.Namespace) -> int:
    base, named = _fhir_base(args)
    # Read-only, so the default server is allowed; credentials only go to a
    # server the user named.
    headers = _fhir_headers() if named else {}
    if named:
        _warn_plain_http(base, headers)
    with HapiClient(base, extra_headers=headers) as client:
        summary = client.capability_summary()
        print(f"server: {safe_url(base)}")
        print(f"  HTTP {summary['status_code']}")
        if summary["status_code"] != 200:
            # Status and OperationOutcome codes only; the body is not echoed.
            hint = (
                f"Set {FHIR_APIKEY_ENV} or {FHIR_TOKEN_ENV} if the server expects one."
                if summary["status_code"] in (401, 403)
                else ""
            )
            err = FhirPostError(
                summary["status_code"], f"{safe_url(base)}/metadata", summary.get("body", "")
            )
            err.hint = hint
            raise err
        # The server wrote these values; strip terminal control characters.
        software = f"{summary.get('software')} {summary.get('software_version')}"
        print(f"  software: {printable(software)}")
        print(f"  fhirVersion: {printable(summary.get('fhir_version'))}")
        for resource in ("Patient", "ServiceRequest", "DiagnosticReport", "ImagingStudy"):
            total = client.search_total(resource)
            print(f"  {resource} on server: {total}")
    return EXIT_OK


def cmd_fhir_sync(args: argparse.Namespace) -> int:
    base, named = _fhir_base(args)
    if not named:
        # sync writes; it never picks a server on its own.
        raise UsageError(
            "fhir sync writes to a FHIR server, so it needs one named.",
            hint=f"Pass --fhir-base URL or set {FHIR_BASE_ENV}.",
        )
    registry = _registry(args)
    conn = _report_db(args)
    headers = _fhir_headers()
    _warn_plain_http(base, headers)
    with HapiClient(base, extra_headers=headers) as client:
        _require_metadata(client, base)
        report = sync_to_fhir(conn, registry, client)
    print(render_sync_text(report))
    return EXIT_FINDINGS if report.skipped else EXIT_OK


# -- Data ------------------------------------------------------------------------------


def cmd_load_studies(args: argparse.Namespace) -> int:
    dicom = Path(args.dicom)
    if not dicom.is_dir():
        raise UsageError(f"--dicom {dicom} is not a folder.", hint="Try: hl7bridge synth --out DIR")
    registry = _registry(args)
    conn = connect(_db_path(args))
    counts = load_studies(conn, registry, dicom)
    print(f"studies loaded: inserted={counts['inserted']} replaced={counts['replaced']}")
    return EXIT_OK


def cmd_load_outbound(args: argparse.Namespace) -> int:
    log_path = Path(args.path)
    if not log_path.is_file():
        raise UsageError(f"--path {log_path} is not a file.")
    registry = _registry(args)
    conn = connect(_db_path(args))
    n = load_outbound(conn, registry, log_path)
    print(f"outbound report entries loaded: {n}")
    return EXIT_OK


def cmd_profile_show(args: argparse.Namespace) -> int:
    registry = _registry(args)
    wanted = args.hospital or sorted(registry.by_id)
    unknown = [h for h in wanted if h not in registry.by_id]
    if unknown:
        raise UsageError(
            f"no profile with hospital_id {unknown[0]!r}.",
            hint=f"Known: {', '.join(sorted(registry.by_id))}.",
        )
    shown = {h: profile_as_dict(registry.by_id[h]) for h in wanted}
    if args.json:
        _print_json(shown)
        return EXIT_OK
    for i, hosp in enumerate(wanted):
        if i:
            print("---")
        print(f"# {hosp}: effective values, defaults filled in")
        print(yaml.safe_dump(shown[hosp], sort_keys=False, default_flow_style=None).rstrip())
    return EXIT_OK


def cmd_profile_check(args: argparse.Namespace) -> int:
    folder = Path(args.folder) if args.folder else _profiles(args).path
    results = check_profile_dir(folder)
    bad = [r for r in results if r.problem]
    if args.json:
        _print_json({"folder": str(folder), "ok": not bad, "files": results})
    else:
        for r in results:
            if r.problem:
                print(f"FAIL {r.name}: {r.problem}")
            else:
                print(f"OK   {r.name} ({r.hospital_id})")
        print(f"{len(results) - len(bad)} of {len(results)} OK in {folder}")
    return EXIT_FINDINGS if bad else EXIT_OK


# -- the parser ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Command:
    name: str  # "fhir sync" for a nested command
    group: str
    summary: str
    func: Callable[[argparse.Namespace], int]
    examples: tuple[str, ...]
    old_name: str | None = None


COMMANDS: tuple[Command, ...] = (
    Command(
        "demo",
        "Try it",
        "run the whole pipeline on synthetic data in one command",
        cmd_demo,
        (
            "hl7bridge demo",
            "hl7bridge demo --keep",
            "hl7bridge demo --fhir-base http://localhost:8080/fhir",
        ),
    ),
    Command(
        "init",
        "Try it",
        "write sample profiles and sample messages into a folder",
        cmd_init,
        ("hl7bridge init", "hl7bridge init my-site"),
    ),
    Command(
        "doctor",
        "Try it",
        "check Python, profiles, database, port and the FHIR server",
        cmd_doctor,
        ("hl7bridge doctor", "hl7bridge doctor --port 2576 --json"),
    ),
    Command(
        "lint",
        "Messages",
        "check messages against the hospital profiles",
        cmd_lint,
        ("hl7bridge lint hl7bridge/samples", "hl7bridge lint msgs/a.hl7 --json"),
    ),
    Command(
        "send",
        "Messages",
        "send one file or a folder over MLLP and show the ACKs",
        cmd_send,
        ("hl7bridge send hl7bridge/samples", "hl7bridge send a.hl7 --port 2576 --timeout 5"),
    ),
    Command(
        "synth",
        "Messages",
        "generate a synthetic day with a defect manifest",
        cmd_synth,
        ("hl7bridge synth --out data/synth", "hl7bridge synth --out data/synth --seed 7"),
    ),
    Command(
        "listen",
        "Listener",
        "run the MLLP listener",
        cmd_listen,
        ("hl7bridge listen", "hl7bridge listen --port 2576 --db data/bridge.db"),
        old_name="serve",
    ),
    Command(
        "reconcile",
        "Reports",
        "orders versus imaging studies",
        cmd_reconcile,
        ("hl7bridge reconcile", "hl7bridge reconcile --now 2026-10-08T12:00:00+00:00 --json"),
    ),
    Command(
        "ack-report",
        "Reports",
        "delivery status of outbound reports",
        cmd_ack_report,
        ("hl7bridge ack-report", "hl7bridge ack-report --now 2026-10-08T12:00:00+00:00"),
    ),
    Command(
        "tat",
        "Reports",
        "turnaround-time statistics for a month",
        cmd_tat,
        ("hl7bridge tat --month 2026-10", "hl7bridge tat --month 2026-10 --json"),
    ),
    Command(
        "summary",
        "Reports",
        "message and quarantine counters",
        cmd_summary,
        ("hl7bridge summary", "hl7bridge summary --db data/bridge.db --json"),
    ),
    Command(
        "fhir sync",
        "FHIR",
        "post the store to a FHIR server",
        cmd_fhir_sync,
        ("hl7bridge fhir sync --fhir-base http://localhost:8080/fhir",),
        old_name="fhir-sync",
    ),
    Command(
        "fhir probe",
        "FHIR",
        "read-only server check: capability statement and resource counts",
        cmd_fhir_probe,
        ("hl7bridge fhir probe", "hl7bridge fhir probe --fhir-base https://example.test/fhir"),
        old_name="fhir-probe",
    ),
    Command(
        "load studies",
        "Data",
        "load DICOM study metadata into the store",
        cmd_load_studies,
        ("hl7bridge load studies --dicom data/synth/dicom",),
        old_name="load-studies",
    ),
    Command(
        "load outbound",
        "Data",
        "load the outbound report log into the store",
        cmd_load_outbound,
        ("hl7bridge load outbound --path data/synth/outbound_reports.jsonl",),
        old_name="load-outbound",
    ),
    Command(
        "profile show",
        "Data",
        "print the effective profile values, defaults filled in",
        cmd_profile_show,
        ("hl7bridge profile show", "hl7bridge profile show HOSPB"),
    ),
    Command(
        "profile check",
        "Data",
        "validate a folder of profiles",
        cmd_profile_check,
        ("hl7bridge profile check", "hl7bridge profile check hl7bridge/profiles"),
    ),
)

GROUPS = ("Try it", "Messages", "Listener", "Reports", "FHIR", "Data")
PARENT_SUMMARIES = {
    "fhir": "FHIR server commands: sync, probe",
    "load": "load DICOM metadata or the outbound report log: studies, outbound",
    "profile": "profile commands: show, check",
}

EXIT_CODES_TEXT = f"""exit codes:
  {EXIT_OK}    success
  {EXIT_FINDINGS}    findings, rejected messages or a failed check
  {EXIT_USAGE}    usage or configuration error
  {EXIT_CONNECTION}    a listener or server did not answer
  {EXIT_INTERNAL}    internal error (a bug; please report it)"""

DEFAULTS_TEXT = """defaults:
  --config  HL7BRIDGE_CONFIG, then ./hl7bridge/profiles, then the bundled profiles
  --db      HL7BRIDGE_DB, then ./hl7bridge.db
  FHIR      --fhir-base, then HL7BRIDGE_FHIR_BASE; probe and doctor fall back to
            http://localhost:8080/fhir, fhir sync needs one named, demo uses only the flag
  secrets   HL7BRIDGE_FHIR_APIKEY and HL7BRIDGE_FHIR_TOKEN, from the environment only"""


def _grouped_commands() -> str:
    width = max(len(c.name) for c in COMMANDS) + 2
    lines: list[str] = []
    for group in GROUPS:
        lines.append(group)
        for c in COMMANDS:
            if c.group == group:
                lines.append(f"  {c.name.ljust(width)}{c.summary}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _common_options() -> argparse.ArgumentParser:
    """-v, -q and --debug, accepted before or after the command. SUPPRESS
    keeps a subparser from resetting a value given before the command."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,
        help="more log output (-vv for debug logs)",
    )
    common.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="only errors on stderr; no 'using ...' lines",
    )
    common.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="debug logs, and stack frames on errors (never message values)",
    )
    return common


def _add_config(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config",
        metavar="DIR",
        help="profile folder (default: HL7BRIDGE_CONFIG, ./hl7bridge/profiles, bundled)",
    )


def _add_db(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db", metavar="FILE", help="database file (default: HL7BRIDGE_DB, ./hl7bridge.db)"
    )


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="print JSON instead of text, for scripts")


def _add_fhir_base(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--fhir-base",
        metavar="URL",
        help=f"FHIR base URL (default: {FHIR_BASE_ENV}, then {DEFAULT_FHIR_BASE})",
    )


def _add_now(p: argparse.ArgumentParser) -> None:
    p.add_argument("--now", default=None, help="report time, ISO 8601 with offset (default: now)")


def _add_message_path(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", nargs="?", help="an .hl7 file, or a folder searched for *.hl7")
    p.add_argument("--dir", help=argparse.SUPPRESS)  # the 0.2 spelling


def _configure_command(name: str, p: argparse.ArgumentParser) -> None:
    """The options each command takes."""
    if name == "demo":
        _add_config(p)
        p.add_argument(
            "--data-dir",
            metavar="DIR",
            help="a new or empty folder to write into and keep (default: a temporary folder"
            " the demo removes after a clean run)",
        )
        p.add_argument("--keep", action="store_true", help="keep the temporary data folder")
        p.add_argument("--seed", type=int, default=42, help="generator seed (default: 42)")
        p.add_argument(
            "--fhir-base", metavar="URL", help="also sync to this FHIR server (default: skip FHIR)"
        )
        p.add_argument(
            "--fhir-wait",
            type=float,
            default=0.0,
            metavar="SECONDS",
            help="wait this long for the FHIR server to answer",
        )
    elif name == "init":
        p.add_argument(
            "folder",
            nargs="?",
            default=str(DEFAULT_FOLDER),
            help="target folder (default: ./hl7bridge)",
        )
        p.add_argument("--force", action="store_true", help="overwrite files that already exist")
    elif name == "doctor":
        _add_config(p)
        _add_db(p)
        p.add_argument(
            "--host", default="127.0.0.1", help="listener host to check (default: 127.0.0.1)"
        )
        p.add_argument(
            "--port", type=_port, default=2575, help="listener port to check (default: 2575)"
        )
        _add_fhir_base(p)
        _add_json(p)
    elif name == "lint":
        _add_message_path(p)
        _add_config(p)
        _add_json(p)
    elif name == "send":
        _add_message_path(p)
        p.add_argument("--host", default="127.0.0.1", help="listener host (default: 127.0.0.1)")
        p.add_argument("--port", type=_port, default=2575, help="listener port (default: 2575)")
        p.add_argument(
            "--timeout",
            type=float,
            default=10.0,
            metavar="SECONDS",
            help="wait this long for each ACK (default: 10)",
        )
    elif name == "synth":
        p.add_argument("--out", required=True, metavar="DIR", help="output folder")
        _add_config(p)
        p.add_argument("--seed", type=int, default=42, help="generator seed (default: 42)")
    elif name == "listen":
        _add_db(p)
        _add_config(p)
        p.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
        p.add_argument("--port", type=_port, default=2575, help="port to bind (default: 2575)")
    elif name in ("reconcile", "ack-report"):
        _add_db(p)
        _add_config(p)
        _add_now(p)
        _add_json(p)
    elif name == "tat":
        _add_db(p)
        _add_config(p)
        p.add_argument(
            "--month", required=True, type=_month, help="YYYY-MM, in hospital local time"
        )
        _add_json(p)
    elif name == "summary":
        _add_db(p)
        _add_json(p)
    elif name == "fhir sync":
        _add_db(p)
        _add_config(p)
        _add_fhir_base(p)
    elif name == "fhir probe":
        _add_fhir_base(p)
    elif name == "load studies":
        _add_db(p)
        _add_config(p)
        p.add_argument(
            "--dicom",
            required=True,
            metavar="DIR",
            help="folder of DICOM files, one subfolder per hospital",
        )
    elif name == "load outbound":
        _add_db(p)
        _add_config(p)
        p.add_argument(
            "--path", required=True, metavar="FILE", help="outbound report log (JSON lines)"
        )
    elif name == "profile show":
        _add_config(p)
        p.add_argument("hospital", nargs="*", help="hospital ids (default: all)")
        _add_json(p)
    elif name == "profile check":
        p.add_argument("folder", nargs="?", help="profile folder (default: as --config)")
        _add_config(p)
        _add_json(p)
    else:  # pragma: no cover - guarded by test_every_command_is_configured
        raise AssertionError(f"no options for {name}")


def _deprecated(command: Command) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        print(
            f"warning: '{command.old_name}' is now 'hl7bridge {command.name}';"
            f" the old name goes away in {REMOVED_IN}.",
            file=sys.stderr,
        )
        return command.func(args)

    return run


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="hl7bridge",
        usage="hl7bridge [-h] [--version] [-v] [-q] [--debug] <command> ...",
        description=(
            "HL7 v2, DICOM and FHIR integration toolkit. Synthetic data only.\n\n"
            + _grouped_commands()
        ),
        epilog=(
            f"{DEFAULTS_TEXT}\n\n{EXIT_CODES_TEXT}\n\n"
            "Run 'hl7bridge <command> --help' for examples."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"hl7bridge {__version__}")
    sub = parser.add_subparsers(
        dest="command", metavar="<command>", required=True, help=argparse.SUPPRESS
    )

    nested: dict[str, argparse._SubParsersAction[argparse.ArgumentParser]] = {}

    def leaf(
        container: argparse._SubParsersAction[argparse.ArgumentParser],
        name: str,
        command: Command,
        func: Callable[[argparse.Namespace], int],
        prog_name: str,
    ) -> None:
        examples = "\n".join(f"  {e}" for e in command.examples)
        p = container.add_parser(
            name,
            help=command.summary,
            description=f"{command.summary}.",
            epilog=f"examples:\n{examples}",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            parents=[common],
            prog=f"hl7bridge {prog_name}",
        )
        _configure_command(command.name, p)
        p.set_defaults(func=func, command=command.name)

    for command in COMMANDS:
        if " " in command.name:
            parent_name, child = command.name.split(" ", 1)
            if parent_name not in nested:
                parent = sub.add_parser(
                    parent_name,
                    help=PARENT_SUMMARIES[parent_name],
                    description=PARENT_SUMMARIES[parent_name],
                    parents=[common],
                    prog=f"hl7bridge {parent_name}",
                )
                nested[parent_name] = parent.add_subparsers(
                    dest="subcommand", metavar="<subcommand>", required=True
                )
            leaf(nested[parent_name], child, command, command.func, command.name)
        else:
            leaf(sub, command.name, command, command.func, command.name)
    for command in COMMANDS:
        if command.old_name:
            leaf(sub, command.old_name, command, _deprecated(command), command.old_name)
    return parser


# -- the error boundary ----------------------------------------------------------------


def _frames(exc: BaseException) -> str:
    """Stack frames: file, line and code. Never the exception text."""
    return "".join(traceback.format_tb(exc.__traceback__)).rstrip()


USER_FS_ERRORS = (
    FileNotFoundError,
    NotADirectoryError,
    IsADirectoryError,
    FileExistsError,
    PermissionError,
)


def _report_error(exc: BaseException, *, debug: bool) -> int:
    """One ``error:`` line per failure, then the hint. No chained causes: a
    cause can quote the rejected input. A BridgeError whose class can quote
    input, and any exception from outside hl7bridge, print the class name
    only. ``--debug`` adds the stack frames, which are code, not data."""
    if isinstance(exc, BridgeError) and not exc.quotes_input:
        text = str(exc)
        if exc.hint:
            text = f"{text} {exc.hint}"
        print(f"error: {text}", file=sys.stderr)
        status = exc.exit_code
    elif isinstance(exc, BridgeError):
        print(
            f"error: {type(exc).__name__} (message withheld: it can quote input)",
            file=sys.stderr,
        )
        status = exc.exit_code
    elif isinstance(exc, USER_FS_ERRORS):
        # A path that is missing, of the wrong kind or not allowed. The file
        # name is not printed: it can be a name read from a folder (a DICOM
        # file is named by its study UID).
        print(
            f"error: {type(exc).__name__}: {exc.strerror or 'a file or folder cannot be used'}."
            " Check the paths you passed.",
            file=sys.stderr,
        )
        status = EXIT_USAGE
    else:
        print(
            f"error: internal error {type(exc).__name__} (message withheld: it can quote"
            " input). This is a bug in hl7bridge; please report it with the --debug output.",
            file=sys.stderr,
        )
        status = EXIT_INTERNAL
    if debug:
        frames = _frames(exc)
        if frames:
            print(frames, file=sys.stderr)
    elif isinstance(exc, USER_FS_ERRORS):
        pass
    elif not isinstance(exc, BridgeError) or exc.quotes_input:
        print("Run again with --debug to see where it failed.", file=sys.stderr)
    return status


def _log_level(args: argparse.Namespace) -> int:
    if getattr(args, "debug", False):
        return logging.DEBUG
    if getattr(args, "quiet", False):
        return logging.ERROR
    verbose = getattr(args, "verbose", 0) or 0
    if verbose >= 2:
        return logging.DEBUG
    if verbose == 1 or args.command == "listen":
        return logging.INFO
    return logging.WARNING


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # httpx, httpcore and pydicom stay at WARNING whatever the level, and URL
    # queries and user info are stripped from every record. See logsafety.py.
    configure_logging(_log_level(args))
    debug = bool(getattr(args, "debug", False))
    try:
        result: int = args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001  CLI boundary, reported below
        return _report_error(exc, debug=debug)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
