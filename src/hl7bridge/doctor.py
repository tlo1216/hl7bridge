"""``hl7bridge doctor``: check the setup and say how to fix what is wrong.

Every check is read-only. The database is opened read-only and never
created. The port check binds and closes a socket. The FHIR check sends one
GET for ``/metadata``, with the credentials from the environment only when
the server was named (``--fhir-base`` or ``HL7BRIDGE_FHIR_BASE``). For the
default server any answer other than 200 is a warning, not a failure.
"""

from __future__ import annotations

import errno
import importlib.util
import platform
import socket
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .config import HospitalRegistry
from .errors import ConfigError
from .fhir import HapiClient
from .logsafety import safe_url
from .profiles import Resolved
from .store.db import SCHEMA_VERSION, inspect_db

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def line(self) -> str:
        text = f"[{self.status}] {self.name}: {self.detail}"
        return f"{text} Fix: {self.fix}" if self.fix else text


def check_python() -> Check:
    version = platform.python_version()
    # pyproject requires 3.12, so an install always passes; the line is
    # there so a bug report shows the version.
    return Check("python", OK, version)


def check_version() -> Check:
    return Check("hl7bridge", OK, __version__)


def check_profiles(resolved: Resolved) -> tuple[Check, HospitalRegistry | None]:
    try:
        registry = HospitalRegistry.load_dir(resolved.path)
    except ConfigError as exc:
        fix = exc.hint or f"run: hl7bridge profile check {resolved.path}"
        return Check("profiles", FAIL, f"{exc} [{resolved.source}]", fix), None
    ids = ", ".join(sorted(registry.by_id))
    detail = f"{len(registry.by_id)} valid ({ids}) in {resolved.describe()}"
    return Check("profiles", OK, detail), registry


def check_timezones(registry: HospitalRegistry | None) -> Check:
    zones = {"UTC"} | ({h.timezone for h in registry.by_id.values()} if registry else set())
    missing = []
    for zone in sorted(zones):
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError:
            missing.append(zone)
    source = "the tzdata package" if _has_tzdata() else "the system time zone database"
    if missing:
        return Check(
            "time zones",
            FAIL,
            f"not found: {', '.join(missing)}",
            "python -m pip install tzdata",
        )
    return Check("time zones", OK, f"{len(zones)} zones found in {source}")


def _has_tzdata() -> bool:
    return importlib.util.find_spec("tzdata") is not None


def check_database(resolved: Resolved) -> Check:
    state, version = inspect_db(resolved.path)
    where = resolved.describe()
    if state == "missing":
        return Check(
            "database",
            OK,
            f"{where} does not exist yet; hl7bridge listen or hl7bridge load creates it",
        )
    if state == "ok":
        return Check("database", OK, f"{where}, schema version {version}")
    if state == "old_schema":
        return Check(
            "database",
            FAIL,
            f"{where} has schema version {version}; this hl7bridge needs version {SCHEMA_VERSION}",
            "there is no migration. Delete it if it is a demo database,"
            " or pass --db with a new path.",
        )
    if state == "no_schema":
        return Check(
            "database",
            WARN,
            f"{where} is a SQLite file with no hl7bridge schema",
            "pass --db with another path unless this file is meant for hl7bridge.",
        )
    return Check(
        "database",
        FAIL,
        f"{where} is not a SQLite database",
        "pass --db with another path.",
    )


def check_port(host: str, port: int) -> Check:
    name = f"port {host}:{port}"
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEACCES", -1)):
            return Check(
                "listen port",
                WARN,
                f"{name} is in use (a listener may already be running)",
                f"stop it, or use --port {port + 1} with listen.",
            )
        return Check(
            "listen port", FAIL, f"{name} cannot be used ({exc.strerror})", "try --host 127.0.0.1."
        )
    finally:
        sock.close()
    return Check("listen port", OK, f"{name} is free")


def check_fhir(
    base: str,
    named: bool,
    headers: dict[str, str],
    client_factory: Callable[..., HapiClient] = HapiClient,
) -> Check:
    shown = safe_url(base)
    with client_factory(base, extra_headers=headers, timeout=5.0) as client:
        status = client.metadata_status()
    if status == 200:
        return Check("fhir", OK, f"{shown}/metadata answers")
    if status == 0:
        if not named:
            return Check(
                "fhir",
                WARN,
                f"no answer from {shown}/metadata (only fhir sync and demo --fhir-base need it)",
                "start a server, or pass --fhir-base, or set HL7BRIDGE_FHIR_BASE.",
            )
        return Check(
            "fhir", FAIL, f"no answer from {shown}/metadata", "start the server or check the URL."
        )
    if not named:
        # Nobody asked for this server: something else may own the port.
        return Check(
            "fhir",
            WARN,
            f"{shown}/metadata answered HTTP {status}; it may not be a FHIR server",
            "pass --fhir-base, or set HL7BRIDGE_FHIR_BASE, to check the server you use.",
        )
    fix = "set HL7BRIDGE_FHIR_APIKEY or HL7BRIDGE_FHIR_TOKEN." if status in (401, 403) else ""
    return Check("fhir", FAIL, f"{shown}/metadata answered HTTP {status}", fix)


def run_checks(
    *,
    profiles: Resolved,
    db: Resolved,
    host: str,
    port: int,
    fhir_base: str,
    fhir_named: bool,
    fhir_headers: dict[str, str],
    client_factory: Callable[..., HapiClient] = HapiClient,
) -> list[Check]:
    profile_check, registry = check_profiles(profiles)
    return [
        check_python(),
        check_version(),
        profile_check,
        check_timezones(registry),
        check_database(db),
        check_port(host, port),
        check_fhir(fhir_base, fhir_named, fhir_headers, client_factory),
    ]
