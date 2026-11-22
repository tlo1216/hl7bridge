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
import math
import os
import re
import signal
import socket
import sqlite3
import ssl
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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
    AckProtocolError,
    BridgeError,
    ConfigError,
    ConnectError,
    FhirPostError,
    HL7ParseError,
    ListenError,
    MappingError,
    NetworkError,
    TlsHandshakeError,
    UsageError,
)
from .explain import as_json, explain_message, render_text, split_messages
from .fhir import HapiClient, render_sync_text, sync_to_fhir
from .fhir.post import check_run_order, post_mapped, refuse_match_only
from .fhir.reset import execute_reset, plan_reset
from .fhir.runscope import DEFAULT_LAB_TAG, RunScope, check_run_id
from .fromfhir import patient_messages, report_message
from .ingest import IngestPipeline, send_messages, serve
from .ingest.ack import ALL_REASONS
from .ingest.direct import DirectHandler, DirectLog, check_profiles_for_direct
from .ingest.loaders import load_outbound, load_studies
from .ingest.mllp import (
    MAX_CONNECTIONS,
    MAX_CONNECTIONS_PER_ADDRESS,
    MAX_PENDING_PER_ADDRESS,
    NEGATIVE_ACK_CODES,
    SHUTDOWN_GRACE_S,
    SendProgress,
    client_ssl_context,
    handshake_failures,
    has_control_id,
    log_handshake_total,
    server_ssl_context,
    shutdown_server,
)
from .ingest.parser import bounded_field, control_id_of, parse_hl7
from .jsonout import dumps, to_jsonable
from .lint import lint_directory, render_lint_text
from .logsafety import NOTICE, configure_logging, printable, safe_url
from .msgfiles import display_key, hl7_files
from .profiles import Resolved, resolve_db, resolve_profiles
from .reconcile import reconcile, render_reconcile_text
from .scaffold import DEFAULT_FOLDER, init_folder
from .store import connect, message_counts, quarantine_summary, utc_iso
from .store.db import inspect_db, schema_mismatch_message
from .synth import generate, verify_manifest
from .tat import render_tat_text, tat_report
from .v2fhir import MappedMessage, bundle_json, hooks, json_text, message_to_bundle

log = logging.getLogger(__name__)

DEFAULT_FHIR_BASE = "http://localhost:8080/fhir"
FHIR_BASE_ENV = "HL7BRIDGE_FHIR_BASE"
FHIR_APIKEY_ENV = "HL7BRIDGE_FHIR_APIKEY"
FHIR_TOKEN_ENV = "HL7BRIDGE_FHIR_TOKEN"
RUN_ID_ENV = "HL7BRIDGE_RUN_ID"
DEFAULT_DIRECT_DB = "hl7bridge-direct.db"
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


def _now_arg(value: str | None, flag: str = "--now") -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise UsageError(f"{flag} {value!r} is not an ISO 8601 timestamp.") from None
    if dt.tzinfo is None:
        raise UsageError(
            f"{flag} {value!r} has no UTC offset.",
            hint=f"Try {flag} 2026-10-08T12:00:00+00:00",
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


STDIN = "-"
STDIN_NAME = "<stdin>"


def _message_path(args: argparse.Namespace) -> Path:
    given = args.path or args.dir
    if not given:
        raise UsageError(f"{args.command}: give a file or folder of *.hl7 messages.")
    if given == STDIN:
        raise UsageError(f"{args.command} does not read stdin; give a file or folder.")
    return Path(given)


@dataclass(frozen=True, slots=True)
class _Input:
    """One input file: its name for reports, its bytes, and its path."""

    name: str
    data: bytes
    path: Path | None


def _reads_stdin(args: argparse.Namespace) -> bool:
    return bool((args.path or args.dir) == STDIN)


def _inputs(args: argparse.Namespace) -> list[_Input]:
    """The files a message command was pointed at, or stdin for ``-``."""
    if _reads_stdin(args):
        return [_Input(STDIN_NAME, sys.stdin.buffer.read(), None)]
    root = _message_path(args)
    return [_Input(display_key(p, root), p.read_bytes(), p) for p in hl7_files(root)]


def _decode(item: _Input) -> str:
    """Message text for display (``explain``). Bytes that are not UTF-8
    become U+FFFD, with a note. Conversion never uses this: two different
    identifiers must not collapse into the same replacement character."""
    try:
        return item.data.decode("utf-8")
    except UnicodeDecodeError:
        print(
            f"note: {item.name} is not valid UTF-8; undecodable bytes are shown as U+FFFD.",
            file=sys.stderr,
        )
        return item.data.decode("utf-8", errors="replace")


def _label(name: str, messages: list[str]) -> list[tuple[str, str]]:
    if len(messages) == 1:
        return [(name, messages[0])]
    return [(f"{name} message {n}", text) for n, text in enumerate(messages, start=1)]


def _labelled_messages(item: _Input) -> list[tuple[str, str]]:
    """(label, text) for each message in a file; a file may hold several."""
    return _label(item.name, split_messages(_decode(item)))


def cmd_explain(args: argparse.Namespace) -> int:
    failed = 0
    documents: list[dict[str, object]] = []
    blocks: list[str] = []
    for item in _inputs(args):
        labelled = _labelled_messages(item)
        if not labelled:
            print(f"error: {item.name}: no HL7 segments in it.", file=sys.stderr)
            failed += 1
            continue
        for n, (label, text) in enumerate(labelled, start=1):
            try:
                views = explain_message(text, show_all=args.all)
            except HL7ParseError:
                # The reason is not printed: say what failed, not what was in it.
                print(f"error: {label}: not parseable as HL7 v2.", file=sys.stderr)
                failed += 1
                continue
            if args.json:
                documents.append({"file": item.name, "message": n, "segments": as_json(views)})
            else:
                blocks.append(f"== {label}\n{render_text(views)}")
    if args.json:
        _print_json(documents)
    elif blocks:
        print("\n\n".join(blocks))
    return EXIT_FINDINGS if failed else EXIT_OK


def _bundle_text(mapped: MappedMessage) -> str:
    # jsontext keeps each decimal's digits (12.50 stays 12.50).
    return json_text(bundle_json(mapped), indent=2) + "\n"


def _bundle_name(label: str) -> str:
    """``a.hl7`` gives ``a.json``; ``sub/b.hl7 message 2`` gives ``sub_b-2.json``."""
    name, _, number = label.partition(" message ")
    stem = name[: -len(".hl7")] if name.endswith(".hl7") else name
    stem = stem.replace("/", "_").strip("<>") or "message"
    return f"{stem}-{number}.json" if number else f"{stem}.json"


def _load_hooks(args: argparse.Namespace) -> None:
    """Install the mapping hooks from --mapping-module, loudly."""
    path = getattr(args, "mapping_module", None)
    if not path:
        return
    names = hooks.load_mapping_module(Path(path))
    print(
        f"warning: loaded mapping hooks ({', '.join(names) or 'none'}) from a Python file."
        " It runs as code inside hl7bridge; use it for a loopback teaching lab only.",
        file=sys.stderr,
    )


def cmd_to_fhir(args: argparse.Namespace) -> int:
    registry = _registry(args)
    _load_hooks(args)
    if args.skip_failures and not args.post:
        raise UsageError("--skip-failures applies to --post only.")
    jobs: list[tuple[str, str]] = []
    failed = undecodable = 0
    for item in _inputs(args):
        try:
            # Strict: an identifier with a bad byte must not become U+FFFD
            # and collide with another one.
            text = item.data.decode("utf-8")
        except UnicodeDecodeError:
            print(f"error: {item.name}: not valid UTF-8; not converted.", file=sys.stderr)
            failed += 1
            undecodable += 1
            continue
        jobs.extend(_label(item.name, split_messages(text)) or [(item.name, "")])
    out_dir = Path(args.out) if args.out else None
    if out_dir is None and not args.post and len(jobs) > 1:
        raise UsageError(
            f"{len(jobs)} messages need a folder for their Bundles.",
            hint="Pass --out DIR, or --post to send them.",
        )
    if out_dir is not None:
        if out_dir.exists() and not out_dir.is_dir():
            raise UsageError(f"--out {out_dir} exists and is not a folder.")
        out_dir.mkdir(parents=True, exist_ok=True)

    mapped: list[tuple[str, MappedMessage]] = []
    for label, text in jobs:
        try:
            mapped.append((label, message_to_bundle(text, registry)))
        except MappingError as exc:
            # A field position and a fixed reason, never a value.
            print(f"error: {label}: {exc}", file=sys.stderr)
            failed += 1
    if args.post and failed and not args.skip_failures:
        # A later message can depend on a failed one (an ORU on its ORM),
        # so nothing is posted unless the user says to go on without it.
        print(
            f"nothing posted: {failed} message(s) could not be mapped;"
            " fix them, or pass --skip-failures to post the rest.",
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    if args.post:
        check_run_order(mapped)

    if out_dir is not None:
        names = [_bundle_name(label) for label, _ in mapped]
        if len(set(names)) != len(names):
            raise UsageError("two inputs would be written to the same file name.")
        for name, (_, result) in zip(names, mapped, strict=True):
            try:
                # Exclusive create: an existing file is never overwritten.
                with open(out_dir / name, "x", encoding="utf-8", newline="\n") as fh:
                    fh.write(_bundle_text(result))
            except FileExistsError:
                raise UsageError(
                    f"{out_dir / name} already exists; to-fhir never overwrites a file.",
                    hint="Pass --out with a new folder.",
                ) from None
        print(f"wrote {len(mapped)} Bundle(s) to {out_dir}", file=sys.stderr)
    elif not args.post:
        for _, result in mapped:
            sys.stdout.write(_bundle_text(result))

    if args.post and mapped:
        _post_bundles(args, mapped)
    if failed:
        print(
            f"{failed} of {len(jobs) + undecodable} message(s) could not be mapped",
            file=sys.stderr,
        )
    return EXIT_FINDINGS if failed else EXIT_OK


def _post_bundles(args: argparse.Namespace, mapped: list[tuple[str, MappedMessage]]) -> None:
    """Post each Bundle as one transaction, in order, through the same post
    as ``fhir sync`` (merges and retirements, ``fhir/post.py``). The first
    one the server does not fully commit stops the run: later messages can
    depend on it. What was committed before it has been printed. A
    match_only profile among them stops the run before anything is sent."""
    for _, result in mapped:
        refuse_match_only(result.hospital_id, result.patient_policy)
    base, named = _fhir_base(args)
    if not named:
        raise UsageError(
            "to-fhir --post writes to a FHIR server, so it needs one named.",
            hint=f"Pass --fhir-base URL or set {FHIR_BASE_ENV}.",
        )
    headers = _fhir_headers()
    _warn_plain_http(base, headers)
    with HapiClient(base, extra_headers=headers) as client:
        _require_metadata(client, base)
        for label, result in mapped:
            entries = post_mapped(client, result)
            counts = Counter(f"{c.kind} {'retired' if c.retired else c.code}" for c in entries)
            summary = ", ".join(f"{key} x{n}" if n > 1 else key for key, n in counts.items())
            print(f"{label}: committed ({summary})")


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
    # Bounded reads: an escape that would expand or cannot be read shows as nothing.
    err_parts = [bounded_field(message, f) or "" for f in ("ERR-3", "ERR-1")]
    code = bounded_field(message, "MSA-1") or ""
    return _AckView(
        code=code if _ACK_CODE.fullmatch(code) else "??",
        text=printable(bounded_field(message, "MSA-3") or ""),
        err=printable(next((p for p in err_parts if p), "")),
    )


@dataclass(frozen=True, slots=True)
class _Outgoing:
    """One message to send: its label in the results, its bytes, and the
    command to suggest when it is not accepted."""

    label: str
    payload: bytes
    next_step: str


def _no_empty_tls_values(args: argparse.Namespace) -> None:
    """An empty value (an unset variable) is a mistake, never "no TLS"."""
    for name in ("tls_ca", "tls_cert", "tls_key"):
        if getattr(args, name, None) == "":
            flag = "--" + name.replace("_", "-")
            raise UsageError(
                f"{flag} is empty.",
                hint="Give a file path. An unset shell variable gives an empty value.",
            )


def _check_host(args: argparse.Namespace) -> None:
    """An empty host (an unset shell variable) is a mistake: a sender would
    connect to this machine and skip the host name check, a listener would
    bind every interface."""
    host = getattr(args, "host", None)
    if host is not None and not host.strip():
        raise UsageError(
            "--host is empty.",
            hint="Give a host name or address. An unset shell variable gives an empty value.",
        )


def _sender_tls(args: argparse.Namespace) -> ssl.SSLContext | None:
    """The TLS context for ``send`` and ``replay``, or None for plain MLLP."""
    _no_empty_tls_values(args)
    ca, cert, key = args.tls_ca, args.tls_cert, args.tls_key
    if not (args.tls or ca is not None or cert is not None or key is not None):
        return None
    if (cert is None) != (key is None):
        raise UsageError("--tls-cert and --tls-key go together.")
    return client_ssl_context(ca, cert, key)


def _listener_tls(args: argparse.Namespace) -> ssl.SSLContext | None:
    """The TLS context for ``listen``, or None for plain MLLP."""
    _no_empty_tls_values(args)
    cert, key, ca = (getattr(args, n, None) for n in ("tls_cert", "tls_key", "tls_ca"))
    if cert is None and key is None and ca is None:
        return None
    if cert is None or key is None:
        raise UsageError("TLS needs both --tls-cert and --tls-key.")
    return server_ssl_context(cert, key, ca)


def _warn_not_loopback(host: str, tls: bool = False) -> None:
    if tls:
        return
    if host not in LOOPBACK_HOSTS:
        print(
            f"warning: sending to {host}, which is not loopback. MLLP has no encryption;"
            " anyone on the network path can read these messages.",
            file=sys.stderr,
        )


def _check_control_ids(outgoing: list[_Outgoing]) -> None:
    """``send`` only: the ACK names a message by MSH-10 alone, so a message
    without one an ACK can name, or two different messages with the same one,
    are refused before anything is sent. The same bytes twice (``--repeat``)
    are one message."""
    seen: dict[str, bytes] = {}
    for item in outgoing:
        if not has_control_id(item.payload):
            raise UsageError(
                f"{item.label} has no usable MSH-10 (control id): it is empty, UNKNOWN,"
                " not UTF-8, or holds an escape other than a separator or hex escape;"
                " nothing sent.",
                hint="Give every message a control id; its ACK is matched by it.",
            )
        first = seen.setdefault(control_id_of(item.payload), item.payload)
        if first != item.payload:
            raise UsageError(
                f"{item.label} has the same MSH-10 (control id) as an earlier message with"
                " other content; nothing sent.",
                hint="Give each message its own control id; its ACK is matched by it.",
            )


def _transmit(
    args: argparse.Namespace,
    outgoing: list[_Outgoing],
    source: str,
    tls: ssl.SSLContext | None,
    *,
    replay: bool = False,
) -> int:
    """Send in order, one ACK at a time, then print the results. Shared by
    ``send`` and ``replay``. Exit 1 when any ACK differs from ``--expect``
    or one is missing; 3 when the listener does not answer in time. A replay
    also sends stored bytes with no control id (a quarantined message): only
    a negative ``UNKNOWN`` ACK answers those."""
    if not replay:
        _check_control_ids(outgoing)
    _warn_not_loopback(args.host, tls is not None)
    acks: list[bytes] = []
    progress = SendProgress()
    where = f"{args.host}:{args.port}"
    interval = 1.0 / args.rate if args.rate else 0.0
    payloads = [o.payload for o in outgoing]
    try:
        asyncio.run(
            send_messages(
                args.host,
                args.port,
                payloads,
                ack_timeout_s=args.timeout,
                acks=acks,
                interval_s=interval,
                ssl_context=tls,
                progress=progress,
                allow_missing_control_id=replay,
            )
        )
    except ConnectError as exc:
        cause = exc.cause
        if isinstance(cause, ConnectionRefusedError):
            reason = "connection refused"
        elif isinstance(cause, socket.gaierror):
            reason = "unknown host"
        elif isinstance(cause, TimeoutError):
            reason = "timed out"
        else:
            reason = (
                "no connection (" + (getattr(cause, "strerror", None) or type(cause).__name__) + ")"
            )
        raise NetworkError(
            f"cannot connect to {where}: {printable(reason)}. Never connected, nothing was sent.",
            hint="Is the listener running? Try: hl7bridge listen",
        ) from None
    except TlsHandshakeError as exc:
        cause = exc.cause
        if isinstance(cause, ssl.SSLCertVerificationError):
            raise NetworkError(
                f"the TLS certificate from {where} was refused: {cause.verify_message}."
                " Nothing was sent.",
                hint="Check --tls-ca and that --host matches a name in the certificate.",
            ) from None
        if isinstance(cause, TimeoutError):  # send_messages says when it is a real stall
            raise NetworkError(
                f"no TLS handshake with {where} within {args.timeout:g} s. Nothing was sent.",
                hint="Is the listener running with --tls-cert?",
            ) from None
        reason = getattr(cause, "reason", None) or type(cause).__name__
        raise NetworkError(
            f"the TLS handshake with {where} failed: {reason}. Nothing was sent.",
            hint="The listener may need a client certificate (--tls-cert, --tls-key),"
            " or may not use TLS.",
        ) from None
    except AckProtocolError as exc:
        _print_send_results(outgoing, acks, source, args, progress.written)
        raise NetworkError(
            f"the reply from {where} was not a valid ACK for the message sent ({exc.reason})"
            f" after {len(acks)} of {len(payloads)} ACKs."
            " The message after the last ACK may or may not have been processed.",
            hint="Only an ACK that answers the message sent counts; this one was not kept.",
        ) from None
    except TimeoutError:
        _print_send_results(outgoing, acks, source, args, progress.written)
        raise NetworkError(
            f"no ACK from {where} within {args.timeout:g} s."
            " The message may or may not have been processed."
        ) from None
    except (ConnectionError, OSError) as exc:  # includes ssl.SSLError after the handshake
        _print_send_results(outgoing, acks, source, args, progress.written)
        raise NetworkError(
            f"the connection to {where} broke ({type(exc).__name__})"
            f" after {len(acks)} of {len(payloads)} ACKs."
            " The message after the last ACK may or may not have been processed.",
            hint="With TLS, a listener that does not use TLS, or that wants a client"
            " certificate, ends the connection like this."
            if tls is not None
            else "",
        ) from None
    return _print_send_results(outgoing, acks, source, args, progress.written)


def _split_frames(payload: bytes) -> list[bytes]:
    """One payload per message: a new message starts at each segment that
    begins with MSH. Bytes before the first MSH are refused (None)."""
    segments = [s for s in payload.split(b"\r") if s]
    if not segments:
        return []
    if not segments[0].startswith(b"MSH"):
        raise ValueError("does not start with an MSH segment")
    frames: list[list[bytes]] = []
    for segment in segments:
        if segment.startswith(b"MSH"):
            frames.append([])
        frames[-1].append(segment)
    return [b"\r".join(f) + b"\r" for f in frames]


def cmd_send(args: argparse.Namespace) -> int:
    _check_host(args)
    tls = _sender_tls(args)  # a bad TLS option fails before any message is read
    outgoing: list[_Outgoing] = []
    for item in _inputs(args):
        payload, converted = _to_cr(item.data)
        if converted:
            print(f"note: {item.name} uses LF line ends; converted to CR for sending.")
        hint = f"hl7bridge lint {item.path}" if item.path else "hl7bridge explain -"
        try:
            frames = _split_frames(payload)
        except ValueError:
            raise UsageError(
                f"{item.name} does not start with an MSH segment; nothing sent.",
                hint=hint.replace("lint", "explain"),
            ) from None
        if not frames:
            raise UsageError(f"{item.name} holds no HL7 segments; nothing sent.")
        # Each message is its own MLLP frame: a listener answers one ACK per
        # frame and would read two messages in one frame as one.
        for n, frame in enumerate(frames, start=1):
            label = item.name if len(frames) == 1 else f"{item.name} message {n}"
            outgoing.append(_Outgoing(label, frame, hint))
    source = STDIN_NAME if _reads_stdin(args) else str(_message_path(args))
    # --repeat sends the same bytes again, never restamped: a listener that
    # keys on the control id answers AA and stores nothing new.
    return _transmit(args, outgoing * args.repeat, source, tls)


def _print_send_results(
    outgoing: list[_Outgoing],
    acks: list[bytes],
    source: str,
    args: argparse.Namespace,
    written: int,
) -> int:
    """``written`` is how many frames reached the connection: a message past
    that was never put on the wire."""
    expect = args.expect
    show_text = bool(getattr(args, "show_ack_text", False))
    counts: Counter[str] = Counter()
    for item, raw in zip(outgoing, acks, strict=False):
        view = _read_ack(raw)
        counts[view.code] += 1
        # The receiver's free text can quote a patient identifier: shown only on
        # request, and only for a negative ACK, whatever --expect says.
        text = show_text and view.code in NEGATIVE_ACK_CODES
        if view.code == expect and not text:
            continue
        words = ACK_CODE_WORDS.get(view.code, "not a known ACK code")
        print(f"{item.label}: {view.code} ({words})")
        if text and view.text:
            print(f"  MSA-3: {view.text}")
        if text and view.err:
            print(f"  ERR: {view.err}")
        reason = view.text.split(":", 1)[0].strip()
        if reason in ALL_REASONS:  # only hl7bridge's own reason codes
            print(f"  quarantine reason: {reason}")
        print(f"  next: {item.next_step}")
    # Messages go one at a time, each after the last ACK: only the one written
    # last, if any, is without an ACK and on the wire.
    for n, item in enumerate(outgoing[len(acks) :], start=len(acks)):
        if n < written:
            print(f"{item.label}: no ACK (it may or may not have been processed)")
        else:
            print(f"{item.label}: not sent")
    print(f"sent {written} message(s) from {source}, got {len(acks)} ACK(s)")
    for code in sorted(counts):
        print(f"  {code}: {counts[code]}")
    if expect != "AA":
        print(f"  expected {expect}: {counts[expect]} of {len(outgoing)}")
    unexpected = sum(n for code, n in counts.items() if code != expect)
    return EXIT_FINDINGS if unexpected or len(acks) != len(outgoing) else EXIT_OK


REPLAY_SOURCES = ("quarantine", "store")
NOT_REPLAYED = "mllp_junk"  # bytes received outside framing are not a message


# The schema versions replay can read. Version 2 has the same messages and
# quarantine tables as version 3, so replaying an old store into a new
# listener is the way to carry its messages over (there is no migration).
REPLAY_SCHEMAS = (2, 3)


def _readonly_db(args: argparse.Namespace) -> sqlite3.Connection:
    """The store, opened read-only: replay never writes to it."""
    path = _db_path(args)
    state, version = inspect_db(path)
    if state == "missing":
        raise UsageError(f"no database at {path}.", hint="Pass --db with the database file.")
    if state == "old_schema" and version not in REPLAY_SCHEMAS:
        raise UsageError(schema_mismatch_message(str(path), version))
    if state == "old_schema":
        state = "ok"
    if state != "ok":
        raise UsageError(f"{path} is not an hl7bridge database.")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _replay_rows(args: argparse.Namespace, conn: sqlite3.Connection) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[object] = []
    if args.source == "quarantine":
        query = "SELECT id, hospital_id, reason AS kind, received_at_utc, raw FROM quarantine"
        where.append("reason != ?")
        params.append(NOT_REPLAYED)
        if args.only_reason:
            where.append("reason = ?")
            params.append(args.only_reason)
    else:
        query = "SELECT id, hospital_id, message_type AS kind, received_at_utc, raw FROM messages"
    if args.since:
        where.append("received_at_utc >= ?")
        params.append(utc_iso(_now_arg(args.since, "--since")))
    if args.hospital:
        where.append("hospital_id = ?")
        params.append(args.hospital)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY received_at_utc, id"
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)
    return conn.execute(query, params).fetchall()


def _stored_bytes(raw: object) -> bytes:
    """The exact stored message: a replay never restamps MSH-7 or MSH-10."""
    if isinstance(raw, (bytes, memoryview)):
        return bytes(raw)
    return str(raw).encode("utf-8")


def cmd_replay(args: argparse.Namespace) -> int:
    if args.only_reason:
        if args.source != "quarantine":
            raise UsageError("--only-reason applies to --from quarantine only.")
        if args.only_reason not in ALL_REASONS or args.only_reason == NOT_REPLAYED:
            known = ", ".join(sorted(ALL_REASONS - {NOT_REPLAYED}))
            raise UsageError(
                "--only-reason takes one of hl7bridge's quarantine reason codes.",
                hint=f"Known: {known}.",
            )
    if args.since:
        _now_arg(args.since, "--since")  # a bad time is a usage error before anything opens
    _check_host(args)
    # The TLS options are checked before the store is read, so an empty
    # selection or a dry run cannot pass a configuration that would fail later.
    tls = _sender_tls(args)
    conn = _readonly_db(args)
    try:
        rows = _replay_rows(args, conn)
    finally:
        conn.close()
    label = "quarantine" if args.source == "quarantine" else "message"
    if not rows:
        print(f"nothing to replay from the {args.source}")
        return EXIT_OK
    # The same bytes quarantined twice (a sender retrying) go out once, the
    # oldest copy: replaying the quarantine into the listener that filled it
    # must not multiply it.
    seen: set[bytes] = set()
    unique: list[sqlite3.Row] = []
    for row in rows:
        raw = _stored_bytes(row["raw"])
        if raw in seen:
            continue
        seen.add(raw)
        unique.append(row)
    duplicates = len(rows) - len(unique)
    rows = unique
    if duplicates:
        print(f"skipping {duplicates} duplicate(s): the same bytes as an older row")
    if args.dry_run:
        for row in rows:
            # Stored fields come from senders; strip control characters.
            print(
                f"{label} #{row['id']}  {printable(row['received_at_utc'])}"
                f"  {printable(row['hospital_id'] or '-')}"
                f"  {printable(row['kind'])}  {len(_stored_bytes(row['raw']))} bytes"
            )
        print(f"{len(rows)} message(s) would be sent, oldest first (dry run: nothing sent)")
        return EXIT_OK
    outgoing = [
        _Outgoing(
            f"{label} #{row['id']}",
            _stored_bytes(row["raw"]),
            f"hl7bridge replay --dry-run --from {args.source}",
        )
        for row in rows
    ]
    return _transmit(args, outgoing, f"the {args.source}", tls, replay=True)


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


def _run_id(args: argparse.Namespace) -> str:
    """--run-id, then HL7BRIDGE_RUN_ID. Required: no run, no start."""
    if getattr(args, "run_id", None):
        return check_run_id(str(args.run_id), "--run-id")
    env = os.environ.get(RUN_ID_ENV)
    if env is not None:
        return check_run_id(env, RUN_ID_ENV)
    raise UsageError(
        "a run id is required: every write and every reset belongs to one run.",
        hint=f"Pass --run-id NAME or set {RUN_ID_ENV}.",
    )


def _lab_tag(registry: HospitalRegistry) -> tuple[str, str]:
    """The lab tag the loaded profiles share (the default when none sets one)."""
    tags = {h.fhir.tag or DEFAULT_LAB_TAG for h in registry.by_id.values()}
    if len(tags) > 1:
        raise ConfigError(
            "the loaded profiles name different fhir.tag values; one run needs one lab tag.",
            hint="Load only the lab's profiles with --config.",
        )
    return tags.pop() if tags else DEFAULT_LAB_TAG


def _named_fhir_base(args: argparse.Namespace, command: str) -> str:
    base, named = _fhir_base(args)
    if not named:
        raise UsageError(
            f"{command} writes to a FHIR server, so it needs one named.",
            hint=f"Pass --fhir-base URL or set {FHIR_BASE_ENV}.",
        )
    return base


def _listen_direct(args: argparse.Namespace, tls: ssl.SSLContext | None) -> int:
    base = _named_fhir_base(args, "listen --target fhir")
    run_id = _run_id(args)
    registry = _registry(args)
    check_profiles_for_direct(registry)
    if getattr(args, "mapping_module", None) and args.host not in LOOPBACK_HOSTS:
        raise UsageError(
            "--mapping-module runs a Python file inside the listener; it is for a loopback"
            " listener only, and --host is not loopback.",
            hint="Leave out --host (loopback is the default), or leave out --mapping-module.",
        )
    _load_hooks(args)
    db_path = Path(args.db) if args.db else Path(DEFAULT_DIRECT_DB)
    _note(args, f"using direct-mode log: {db_path}")
    scope = RunScope(run_id, _lab_tag(registry))
    headers = _fhir_headers()
    _warn_plain_http(base, headers)
    direct_log = DirectLog(db_path)
    idle = True
    try:
        client = HapiClient(base, timeout=args.fhir_timeout, extra_headers=headers)
        try:
            _require_metadata(client, base)
            handler = DirectHandler(registry, client, scope, direct_log)
            where = f"FHIR {safe_url(base)}, run {run_id}, log {db_path}"
            try:
                return _serve_forever(args, handler.handle_frame, handler.handle_junk, where, tls)
            finally:
                idle = handler.close(timeout=SHUTDOWN_GRACE_S)
        finally:
            # A worker still busy may be in a commit: its client and log stay
            # open so it can record the outcome; the process exit waits for it.
            if idle:
                client.close()
    finally:
        if idle:
            direct_log.close()


def _in_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _serve_forever(
    args: argparse.Namespace,
    handle_frame: Callable[[bytes], Awaitable[bytes]],
    handle_junk: Callable[[bytes], Awaitable[None]],
    where: str,
    tls: ssl.SSLContext | None,
) -> int:

    main_thread = _in_main_thread()
    posix = sys.platform != "win32"

    async def run() -> None:
        main = asyncio.current_task()
        if posix and main_thread and main is not None:
            # SIGTERM takes the path of Ctrl+C: the main task is cancelled at
            # an await, and shutdown_server gives a busy client its grace. A
            # repeat while that runs is ignored, so it cannot cut the grace.
            def terminate(task: asyncio.Task[None] = main) -> None:
                if not task.cancelling():
                    task.cancel()

            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, terminate)
        try:
            extra: dict[str, Any] = (
                {
                    "ssl_context": tls,
                    "max_connections": args.max_connections,
                    "max_connections_per_address": args.max_connections_per_address,
                    "max_pending_per_address": args.max_pending_per_address,
                }
                if tls
                else {}
            )
            server = await serve(args.host, args.port, handle_frame, handle_junk, **extra)
        except OSError as exc:
            raise _listen_error(args.host, args.port, exc) from None
        bound = server.sockets[0].getsockname()[1] if server.sockets else args.port
        # Printed only once the socket is bound, so it is never a false claim.
        mode = "MLLP over TLS" if tls else "MLLP"
        if tls and getattr(args, "tls_ca", None):
            mode += " (client certificates required)"
        print(f"{mode} listener on {args.host}:{bound}, {where} (Ctrl+C to stop)", flush=True)
        if args.host not in LOOPBACK_HOSTS and not tls:
            print(
                f"warning: {args.host} is not loopback. MLLP has no encryption or"
                " authentication; other machines can now send to this listener.",
                file=sys.stderr,
            )
        elif args.host not in LOOPBACK_HOSTS and not getattr(args, "tls_ca", None):
            print(
                f"warning: {args.host} is not loopback and no --tls-ca is set. TLS encrypts"
                " the traffic but does not authenticate senders; any machine that can reach"
                " the port can still send to this listener.",
                file=sys.stderr,
            )
        try:
            # serve_forever waits for every client when it is cancelled (Python
            # 3.12), so it runs as its own task: the clients are closed first.
            serving = asyncio.ensure_future(server.serve_forever())
            try:
                await asyncio.shield(serving)
            finally:
                # Idle clients are closed at once, a busy one gets a grace period.
                await shutdown_server(server)
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
        except OSError as exc:
            raise NetworkError(
                f"the listener on {args.host}:{bound} stopped:"
                f" {exc.strerror or type(exc).__name__}."
            ) from None

    def stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt  # Windows: SIGTERM (raised in-process only) as Ctrl+C

    previous = signal.getsignal(signal.SIGTERM) if main_thread else None
    if not posix and main_thread:
        signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):  # Ctrl+C, or SIGTERM on POSIX
        failed = handshake_failures()
        print("listener stopped" + (f"; {failed} TLS handshake(s) failed" if failed else ""))
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        log_handshake_total()  # any shutdown, not only Ctrl+C
    return EXIT_OK


def cmd_listen(args: argparse.Namespace) -> int:
    _check_host(args)  # an empty host would bind every interface
    tls = _listener_tls(args)  # before any store is opened or server contacted
    if args.target == "fhir":
        return _listen_direct(args, tls)
    for flag in ("run_id", "mapping_module", "fhir_base"):
        if getattr(args, flag, None):
            raise UsageError(f"--{flag.replace('_', '-')} applies to --target fhir only.")
    registry = _registry(args)
    db_path = _db_path(args)
    conn = connect(db_path)
    pipeline = IngestPipeline(registry, conn)
    try:
        return _serve_forever(
            args, pipeline.handle_frame, pipeline.handle_junk, f"db {db_path}", tls
        )
    finally:
        conn.close()


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


def cmd_fhir_reset(args: argparse.Namespace) -> int:
    base = _named_fhir_base(args, "fhir reset")
    run_id = _run_id(args)
    registry = _registry(args)
    scope = RunScope(run_id, _lab_tag(registry))
    direct_db = Path(args.db) if args.db else None
    if direct_db is not None and not direct_db.is_file():
        raise UsageError("--db names no file; give the listener's direct-mode log.")
    headers = _fhir_headers()
    _warn_plain_http(base, headers)
    with HapiClient(base, extra_headers=headers) as client:
        _require_metadata(client, base)
        plan = plan_reset(client, scope)
        print(f"run {run_id} on {safe_url(base)}:")
        for kind, ids in plan.ids.items():
            print(f"  {kind}: {len(ids)}")
        print("  Patient: never deleted")
        if plan.total == 0:
            print("nothing to delete")
            _void_direct_log(direct_db, run_id, client.base_url, args.yes)
            return EXIT_OK
        if not args.yes:
            print("dry run: nothing deleted. Pass --yes to delete these resources.")
            return EXIT_OK
        print(f"type the run id to delete these {plan.total} resources: ", end="", flush=True)
        try:
            typed = input()
        except EOFError:
            typed = ""
        if typed.strip() != run_id:
            print("not confirmed: nothing deleted.", file=sys.stderr)
            return EXIT_FINDINGS
        deleted = execute_reset(client, plan)
        print(
            f"sent {deleted} deletes for run {run_id}; a resource that left the run since"
            " the count above was kept"
        )
        _void_direct_log(direct_db, run_id, client.base_url, True)
    return EXIT_OK


def _void_direct_log(path: Path | None, run_id: str, base: str, really: bool) -> None:
    """Void the run's rows in the listener's log, so a resend after the
    reset is written again and a changed one is not a conflict."""
    if path is None or not really:
        return
    direct_log = DirectLog(path)
    try:
        voided = direct_log.void_run(run_id, base)
    finally:
        direct_log.close()
    print(f"voided {voided} log row(s) of run {run_id} in {path}")


def cmd_from_fhir(args: argparse.Namespace) -> int:
    base, named = _fhir_base(args)
    headers = _fhir_headers() if named else {}
    registry = _registry(args)
    hospital = registry.by_id.get(args.hospital)
    if hospital is None:
        raise UsageError(
            "--hospital names no loaded profile.",
            hint=f"Known: {', '.join(sorted(registry.by_id))}.",
        )
    now = _now_arg(args.now)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with HapiClient(base, extra_headers=headers) as client:
        _require_metadata(client, base)
        if args.report:
            text, mrn, skipped = report_message(client, args.report, hospital, now)
            files = {"oru_r01_from_report.hl7": text}
        else:
            files, mrn = patient_messages(client, args.patient, hospital, now)
            skipped = 0
    for name, text in files.items():
        with open(out / name, "x", encoding="utf-8", newline="") as handle:
            handle.write(text)
    chosen = "the identifier typed MR" if mrn.typed_mr else "the first identifier (none typed MR)"
    print(f"PID-3 holds {chosen} of the Patient.")
    if skipped:
        print(f"{skipped} result(s) left out: a value type the renderer does not write.")
    print(f"wrote {len(files)} message(s) to {out}")
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
    for hospital in registry.by_id.values():
        refuse_match_only(hospital.hospital_id, hospital.fhir.patient_policy)
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
        "explain",
        "Messages",
        "print a message one field per line, with field names",
        cmd_explain,
        (
            "hl7bridge explain hl7bridge/samples/03_hospa_oru_r01_final.hl7",
            "hl7bridge explain a.hl7 --all",
            "hl7bridge explain hl7bridge/samples --json",
        ),
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
        "send a file, a folder or stdin over MLLP and show the ACKs",
        cmd_send,
        (
            "hl7bridge send hl7bridge/samples",
            "hl7bridge send a.hl7 --port 2576 --timeout 5",
            "hl7bridge send a.hl7 --repeat 3 --rate 2",
            "hl7bridge send broken.hl7 --expect AE",
        ),
    ),
    Command(
        "replay",
        "Messages",
        "resend quarantined or stored messages, oldest first",
        cmd_replay,
        (
            "hl7bridge replay --dry-run",
            "hl7bridge replay --only-reason missing_accession --port 2576",
            "hl7bridge replay --from store --since 2026-10-07T00:00:00+00:00 --rate 5",
        ),
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
        (
            "hl7bridge listen",
            "hl7bridge listen --port 2576 --db data/bridge.db",
            "hl7bridge listen --target fhir --fhir-base http://localhost:8080/fhir"
            " --run-id lab-a --config lab-profiles",
        ),
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
        "to-fhir",
        "FHIR",
        "convert messages to FHIR transaction Bundles (JSON, or --post)",
        cmd_to_fhir,
        (
            "hl7bridge to-fhir hl7bridge/samples/14_hospa_oru_r01_structured.hl7",
            "hl7bridge to-fhir hl7bridge/samples --out bundles",
            "hl7bridge to-fhir a.hl7 --post --fhir-base http://localhost:8080/fhir",
        ),
    ),
    Command(
        "fhir sync",
        "FHIR",
        "post the store to a FHIR server",
        cmd_fhir_sync,
        ("hl7bridge fhir sync --fhir-base http://localhost:8080/fhir",),
    ),
    Command(
        "fhir reset",
        "FHIR",
        "delete one lab run's resources (dry run unless --yes)",
        cmd_fhir_reset,
        (
            "hl7bridge fhir reset --run-id lab-a --fhir-base http://localhost:8080/fhir",
            "hl7bridge fhir reset --run-id lab-a --yes",
        ),
    ),
    Command(
        "from-fhir",
        "FHIR",
        "write sample HL7 v2 messages for a Patient or a report on a FHIR server",
        cmd_from_fhir,
        (
            "hl7bridge from-fhir --patient 123 --hospital WINTEHR --out msgs",
            "hl7bridge from-fhir --report 456 --hospital WINTEHR --out msgs",
        ),
    ),
    Command(
        "fhir probe",
        "FHIR",
        "read-only server check: capability statement and resource counts",
        cmd_fhir_probe,
        ("hl7bridge fhir probe", "hl7bridge fhir probe --fhir-base https://example.test/fhir"),
    ),
    Command(
        "load studies",
        "Data",
        "load DICOM study metadata into the store",
        cmd_load_studies,
        ("hl7bridge load studies --dicom data/synth/dicom",),
    ),
    Command(
        "load outbound",
        "Data",
        "load the outbound report log into the store",
        cmd_load_outbound,
        ("hl7bridge load outbound --path data/synth/outbound_reports.jsonl",),
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
    "fhir": "FHIR server commands: sync, reset, probe",
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
        help="only errors (and TLS security notices) on stderr; no 'using ...' lines",
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


def _add_run_id(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--run-id", metavar="NAME", help=f"the lab run (default: {RUN_ID_ENV}; required)"
    )


def _add_mapping_module(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--mapping-module",
        metavar="FILE",
        help="a .py file of mapping hooks (sex_code, abnormal_flag, observation_time);"
        " teaching labs only",
    )


def _add_now(p: argparse.ArgumentParser) -> None:
    p.add_argument("--now", default=None, help="report time, ISO 8601 with offset (default: now)")


def _add_message_path(p: argparse.ArgumentParser, *, stdin: bool = False) -> None:
    text = "an .hl7 file, or a folder searched for *.hl7"
    p.add_argument("path", nargs="?", help=f"{text}, or - for stdin" if stdin else text)
    p.add_argument("--dir", help=argparse.SUPPRESS)  # the 0.2 spelling


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"{number} is not 1 or more")
    return number


def _rate(value: str) -> float:
    try:
        rate = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not 0 < rate <= 1000:
        raise argparse.ArgumentTypeError(f"{value} is not more than 0 and at most 1000")
    return rate


def _seconds(value: str) -> float:
    """A wait in seconds: a finite number above 0 (nan, inf, 0 and negatives are refused)."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a finite number above 0")
    return seconds


ACK_CODES = ("AA", "AE", "AR", "CA", "CE", "CR")


def _add_sender(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tls",
        action="store_true",
        help="use MLLP over TLS (TLS 1.2 or newer; the certificate and host name are checked;"
        " implied by the other --tls options)",
    )
    p.add_argument(
        "--tls-ca",
        metavar="FILE",
        help="PEM file of the CA that signed the listener's certificate (default: system CAs)",
    )
    p.add_argument(
        "--tls-cert", metavar="FILE", help="PEM client certificate to present (needs --tls-key)"
    )
    p.add_argument("--tls-key", metavar="FILE", help="PEM private key for --tls-cert")
    p.add_argument("--host", default="127.0.0.1", help="listener host (default: 127.0.0.1)")
    p.add_argument("--port", type=_port, default=2575, help="listener port (default: 2575)")
    p.add_argument(
        "--show-ack-text",
        action="store_true",
        help="print the free text of a negative ACK (MSA-3, ERR); it is the receiver's own"
        " words and can quote patient data, so it is off by default",
    )
    p.add_argument(
        "--timeout",
        type=_seconds,
        default=10.0,
        metavar="SECONDS",
        help="wait this long for each ACK (default: 10)",
    )
    p.add_argument(
        "--rate",
        type=_rate,
        default=None,
        metavar="PER_SECOND",
        help="send at most this many messages a second (default: as fast as ACKs come)",
    )
    p.add_argument(
        "--expect",
        choices=ACK_CODES,
        default="AA",
        help="exit 1 when any ACK code differs from this one (default: AA)",
    )


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
    elif name == "explain":
        _add_message_path(p, stdin=True)
        p.add_argument("--all", action="store_true", help="show empty fields and components too")
        _add_json(p)
    elif name == "send":
        _add_message_path(p, stdin=True)
        _add_sender(p)
        p.add_argument(
            "--repeat",
            type=_positive_int,
            default=1,
            metavar="N",
            help="send the whole set N times, unchanged (default: 1)",
        )
    elif name == "replay":
        _add_db(p)
        p.add_argument(
            "--from",
            dest="source",
            choices=REPLAY_SOURCES,
            default="quarantine",
            help="what to resend (default: quarantine)",
        )
        p.add_argument(
            "--only-reason", metavar="REASON", help="only quarantine rows with this reason code"
        )
        p.add_argument(
            "--since", metavar="TIME", help="only rows received at or after this time (ISO 8601)"
        )
        p.add_argument("--hospital", metavar="ID", help="only rows for this hospital id")
        p.add_argument("--limit", type=_positive_int, metavar="N", help="at most N rows")
        p.add_argument(
            "--dry-run", action="store_true", help="list what would be sent and send nothing"
        )
        _add_sender(p)
    elif name == "to-fhir":
        _add_message_path(p, stdin=True)
        _add_config(p)
        p.add_argument("--out", metavar="DIR", help="write one Bundle per message into a folder")
        p.add_argument(
            "--post", action="store_true", help="post each Bundle to a FHIR server, in order"
        )
        p.add_argument(
            "--skip-failures",
            action="store_true",
            help="with --post, post the messages that map even when others do not",
        )
        p.add_argument(
            "--fhir-base", metavar="URL", help=f"the server for --post (default: {FHIR_BASE_ENV})"
        )
        _add_mapping_module(p)
    elif name == "synth":
        p.add_argument("--out", required=True, metavar="DIR", help="output folder")
        _add_config(p)
        p.add_argument("--seed", type=int, default=42, help="generator seed (default: 42)")
    elif name == "listen":
        p.add_argument(
            "--db",
            metavar="FILE",
            help="database file (default: HL7BRIDGE_DB, ./hl7bridge.db; with --target fhir"
            f" the direct-mode log, default ./{DEFAULT_DIRECT_DB})",
        )
        _add_config(p)
        p.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
        p.add_argument("--port", type=_port, default=2575, help="port to bind (default: 2575)")
        p.add_argument(
            "--tls-cert",
            metavar="FILE",
            help="PEM certificate: serve MLLP over TLS 1.2 or newer (needs --tls-key)",
        )
        p.add_argument("--tls-key", metavar="FILE", help="PEM private key for --tls-cert")
        p.add_argument(
            "--tls-ca",
            metavar="FILE",
            help="PEM CA file: require and verify a client certificate from every sender",
        )
        p.add_argument(
            "--max-connections",
            type=_positive_int,
            default=MAX_CONNECTIONS,
            metavar="N",
            help=f"with TLS, at most N connections open at once; more are closed and logged"
            f" (default: {MAX_CONNECTIONS})",
        )
        p.add_argument(
            "--max-connections-per-address",
            type=_positive_int,
            default=MAX_CONNECTIONS_PER_ADDRESS,
            metavar="N",
            help="with TLS, at most N connections open at once from one address"
            f" (default: {MAX_CONNECTIONS_PER_ADDRESS})",
        )
        p.add_argument(
            "--max-pending-per-address",
            type=_positive_int,
            default=MAX_PENDING_PER_ADDRESS,
            metavar="N",
            help="with TLS, at most N handshakes pending at once from one address"
            f" (default: {MAX_PENDING_PER_ADDRESS})",
        )
        p.add_argument(
            "--target",
            choices=("store", "fhir"),
            default="store",
            help="store: the SQLite store (default); fhir: one FHIR transaction per message,"
            " ACK after commit",
        )
        p.add_argument(
            "--fhir-base", metavar="URL", help=f"with --target fhir (default: {FHIR_BASE_ENV})"
        )
        _add_run_id(p)
        p.add_argument(
            "--fhir-timeout",
            type=float,
            default=10.0,
            metavar="SECONDS",
            help="with --target fhir, give up on a FHIR request after this long, then AE"
            " (default: 10)",
        )
        _add_mapping_module(p)
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
    elif name == "fhir reset":
        _add_config(p)
        _add_fhir_base(p)
        _add_run_id(p)
        p.add_argument(
            "--db",
            metavar="FILE",
            help="the listener's direct-mode log: void the run's rows there too",
        )
        p.add_argument(
            "--yes", action="store_true", help="delete after a typed run id (default: dry run)"
        )
    elif name == "from-fhir":
        _add_config(p)
        _add_fhir_base(p)
        which = p.add_mutually_exclusive_group(required=True)
        which.add_argument("--patient", metavar="ID", help="the Patient's server id")
        which.add_argument("--report", metavar="ID", help="a DiagnosticReport's server id")
        p.add_argument("--hospital", required=True, metavar="ID", help="the profile to write for")
        p.add_argument("--out", required=True, metavar="DIR", help="folder for the .hl7 files")
        _add_now(p)
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
        return NOTICE  # hides WARNING and below, keeps TLS security notices and errors
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
