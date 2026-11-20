"""Direct-to-FHIR listener: one message, one transaction, then the ACK.

``hl7bridge listen --target fhir`` handles each frame here instead of the
store pipeline. Flow per message: decode, parse, the routing checks, the
processing id guard, the message identity check, map (``v2fhir``),
resolve the patient (``fhir/match.py``), scope to the run
(``fhir/runscope.py``), take the per-key locks, prepare and commit one
transaction Bundle, check the transaction response, and only then answer.
SQLite holds a log of every arrival (``DirectLog``), which is also the
quarantine: rows with AE or AR keep their raw bytes.

ACK decision table (``ack_code_for``, tested row by row):

| Condition | ACK | Reason |
| --- | --- | --- |
| Not UTF-8, or not parseable as HL7 | AR | parse_error |
| MSH-10 empty | AR | missing_control_id |
| MSH-4/MSH-6 match no hospital | AR | unknown_hospital |
| MSH-9 not in the profile | AR | unexpected_message_type |
| ADT^A08 or ADT^A40 (the Patient is read only) | AR | patient_read_only |
| Any other type direct mode does not write | AR | unsupported_message_type |
| MSH-11 not in ``fhir.allowed_processing_ids`` | AR | refused_processing_id |
| Same identity, different content | AE | control_id_conflict |
| Same identity, same content, committed before | AA | (duplicate, nothing written) |
| The message does not map | AE | mapping_error |
| No Patient with the MRN | AE | unknown_patient |
| Several Patients with the MRN, counted over every page | AE | ambiguous_patient |
| The one Patient is inactive, or replaced by another | AE | inactive_patient, replaced_patient |
| A key matches a resource outside the run | AE | outside_run |
| A key matches several resources | AE | ambiguous_resource |
| The server holds a newer version of the report | AE | stale_result |
| Nothing to write (an ADT with no PV1-19) | AE | nothing_to_write |
| Another writer created a resource first | AE | concurrent_write |
| Server unreachable | AE | fhir_unreachable |
| Timeout, or the answer lost after the request went | AE | fhir_timeout, fhir_response_lost |
| Server answered 4xx or 5xx | AE | fhir_conflict (409, 412), fhir_rejected, fhir_server_error |
| Server answered 200 with no valid transaction response | AE | fhir_unconfirmed |
| Committed, and the response validated | AA | accepted |
| Internal error (server boundary) | AR | internal_error |

Message identity is hospital, MSH-3, MSH-4 and MSH-10 within one run, and
the body hash is compared under it: the same bytes again are a duplicate,
anything else under the same identity is a conflict (MSH-7 included; no
field is ignored). A sender that retries after a lost ACK sends the same
bytes; a changed message gets a new control id.

PHI discipline as in the store pipeline: log lines carry the log row id,
the hospital id, a configured message type, the reason code and the ACK
code. MSH-10, MRNs and every other message value stay out of logs and
out of MSA-3, which holds the reason code and a field position or FHIR
issue codes only.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import sqlite3
import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from ..config import HospitalConfig, HospitalRegistry
from ..errors import FhirPostError, HL7ParseError, MappingError, StoreError
from ..fhir.client import HapiClient
from ..fhir.match import resolve_patient
from ..fhir.post import ConcurrentWriteError, StaleResultError
from ..fhir.runscope import (
    AmbiguousResourceError,
    OutsideRunError,
    RunScope,
    lock_keys,
    post_run,
    scope_body,
)
from ..store.db import utc_iso
from ..v2fhir import message_to_bundle
from .ack import AckCode, AckDecision, build_ack
from .parser import (
    get_control_id,
    get_facilities,
    get_field,
    get_message_type,
    get_version,
    normalize_segments,
    parse_hl7,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

DIRECT_TYPES = ("ADT^A01", "ADT^A04", "ORM^O01", "ORU^R01")
PATIENT_WRITING_TYPES = ("ADT^A08", "ADT^A40")

# Outcome reasons. AA reasons first, then AR, then AE.
COMMITTED = "accepted"
DUPLICATE = "duplicate"
AR_REASONS = frozenset(
    {
        "parse_error",
        "missing_control_id",
        "unknown_hospital",
        "unexpected_message_type",
        "patient_read_only",
        "unsupported_message_type",
        "refused_processing_id",
        "internal_error",
        "mllp_junk",
    }
)
AE_REASONS = frozenset(
    {
        "control_id_conflict",
        "mapping_error",
        "unknown_patient",
        "ambiguous_patient",
        "inactive_patient",
        "replaced_patient",
        "outside_run",
        "ambiguous_resource",
        "stale_result",
        "nothing_to_write",
        "concurrent_write",
        "fhir_unreachable",
        "fhir_timeout",
        "fhir_response_lost",
        "fhir_conflict",
        "fhir_rejected",
        "fhir_server_error",
        "fhir_unconfirmed",
        "commit_not_confirmed",
    }
)
_CONNECT_ERRORS = frozenset({"ConnectError", "ConnectTimeout"})


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened to one message. ``committed`` is set only by the
    path that saw the server's transaction response validate (or a log row
    that says it did, for a duplicate); nothing else can make it true.
    ``field`` is fixed text from the code: a field position or FHIR issue
    codes, never a message value."""

    reason: str
    field: str = ""
    committed: bool = False


def ack_code_for(outcome: Outcome) -> AckDecision:
    """The ACK for an outcome. Commit first, ACK second.

    - AA only for a commit whose transaction response validated: a Bundle
      of type transaction-response, one entry per request, every entry
      200 or 201 (``HapiClient.commit``). A 2xx status alone is not enough,
      because a proxy or a broken server can answer 200 for work that was
      never done, and an AA tells the sender to delete its copy.
    - AE for everything the sender can fix or should retry: a mapping
      error, an unknown or ambiguous patient, a server 4xx, a stale
      result, and the server being unreachable or slow. Unreachable is AE
      and not AR because nothing is wrong with the message; the sender
      keeps it and sends it again later.
    - A lost response is AE even though the server may have committed:
      the listener cannot tell, so it must not claim success. The resend
      is safe because every write is keyed: it updates in place.
    - AR for what no resend can fix: the wrong message type for this
      listener, a refused processing id (MSH-11), unparseable input.
    """
    if outcome.reason in (COMMITTED, DUPLICATE):
        if not outcome.committed:
            return AckDecision("AE", "commit_not_confirmed", ack_text="see log")
        return AckDecision("AA", COMMITTED)
    code: AckCode = "AR" if outcome.reason in AR_REASONS else "AE"
    return AckDecision(code, outcome.reason, ack_text=outcome.field)


def outcome_for_error(exc: Exception) -> Outcome:
    """The outcome for an exception from mapping or posting."""
    if isinstance(exc, MappingError):
        return Outcome("mapping_error", exc.field)
    if isinstance(exc, OutsideRunError):
        return Outcome("outside_run", exc.kind)
    if isinstance(exc, AmbiguousResourceError):
        return Outcome("ambiguous_resource", exc.kind)
    if isinstance(exc, StaleResultError):
        return Outcome("stale_result", "OBR-22")
    if isinstance(exc, ConcurrentWriteError):
        return Outcome("concurrent_write")
    if isinstance(exc, FhirPostError):
        codes = ", ".join(exc.issue_codes)
        issue = f"issue codes {codes}" if codes else ""
        if exc.status_code == 0:
            if exc.transport in _CONNECT_ERRORS:
                return Outcome("fhir_unreachable")
            if exc.transport.endswith("Timeout"):
                return Outcome("fhir_timeout")
            if exc.transport:
                return Outcome("fhir_response_lost")
            return Outcome("fhir_rejected")
        if exc.status_code == 200:
            return Outcome("fhir_unconfirmed")
        if exc.status_code in (409, 412):
            return Outcome("fhir_conflict", issue)
        if exc.status_code >= 500:
            return Outcome("fhir_server_error", issue)
        return Outcome("fhir_rejected", issue)
    raise exc


def body_hash(text: str) -> str:
    """SHA-256 of the whole message. The one normalization is the segment
    terminator (LF and CRLF become CR), which carries no field data."""
    return hashlib.sha256(normalize_segments(text).encode("utf-8")).hexdigest()


# -- the log ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS direct_schema (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS direct_log (
    id               INTEGER PRIMARY KEY,
    run_id           TEXT NOT NULL,
    hospital_id      TEXT,
    sending_app      TEXT NOT NULL DEFAULT '',
    sending_facility TEXT NOT NULL DEFAULT '',
    control_id       TEXT NOT NULL DEFAULT '',
    message_type     TEXT NOT NULL DEFAULT '',
    body_hash        TEXT NOT NULL,
    received_at_utc  TEXT NOT NULL,
    ack_code         TEXT NOT NULL CHECK (ack_code IN ('AA', 'AE', 'AR')),
    reason           TEXT NOT NULL,
    raw              BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS direct_log_identity
    ON direct_log (run_id, hospital_id, sending_app, sending_facility, control_id);
"""
DIRECT_SCHEMA_VERSION = 1
_OWN_TABLES = {"direct_schema", "direct_log"}


@dataclass(frozen=True, slots=True)
class Identity:
    hospital_id: str
    sending_app: str
    sending_facility: str
    control_id: str

    def key(self, run_id: str) -> str:
        return "\x1f".join(
            (
                "identity",
                run_id,
                self.hospital_id,
                self.sending_app,
                self.sending_facility,
                self.control_id,
            )
        )


class DirectLog:
    """The direct-mode message log and quarantine, in its own SQLite file.

    A file that holds any other table (a store database, say) is refused,
    so direct mode never writes into a database it does not own."""

    def __init__(self, path: str | Path) -> None:
        target = str(path)
        if target != ":memory:" and not Path(target).parent.is_dir():
            raise StoreError(
                f"the folder for {target} does not exist.", hint="Create it, or pass another --db."
            )
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        tables = {
            str(r[0])
            for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if tables - _OWN_TABLES:
            self.conn.close()
            raise StoreError(
                f"{target} holds tables that are not a direct-mode log; refusing to write there.",
                hint="Pass --db with a new file, such as hl7bridge-direct.db.",
            )
        self.conn.executescript(_SCHEMA)
        row = self.conn.execute("SELECT version FROM direct_schema").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO direct_schema (version) VALUES (?)", (DIRECT_SCHEMA_VERSION,)
            )
        elif row[0] != DIRECT_SCHEMA_VERSION:
            self.conn.close()
            raise StoreError(f"{target} is a direct-mode log of another version.")

    def close(self) -> None:
        self.conn.close()

    def committed_hash(self, run_id: str, identity: Identity) -> str | None:
        """The body hash of the first committed message with this identity."""
        with self._lock:
            row = self.conn.execute(
                "SELECT body_hash FROM direct_log WHERE run_id = ? AND hospital_id = ?"
                " AND sending_app = ? AND sending_facility = ? AND control_id = ?"
                " AND ack_code = 'AA' ORDER BY id LIMIT 1",
                (
                    run_id,
                    identity.hospital_id,
                    identity.sending_app,
                    identity.sending_facility,
                    identity.control_id,
                ),
            ).fetchone()
        return None if row is None else str(row[0])

    def record(
        self,
        *,
        run_id: str,
        identity: Identity | None,
        message_type: str,
        digest: str,
        received: str,
        decision: AckDecision,
        raw: bytes,
    ) -> int:
        with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO direct_log (run_id, hospital_id, sending_app, sending_facility,"
                " control_id, message_type, body_hash, received_at_utc, ack_code, reason, raw)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    identity.hospital_id if identity else None,
                    identity.sending_app if identity else "",
                    identity.sending_facility if identity else "",
                    identity.control_id if identity else "",
                    message_type,
                    digest,
                    received,
                    decision.code,
                    decision.reason,
                    raw,
                ),
            )
            return int(cursor.lastrowid or 0)


# -- locks -----------------------------------------------------------------------------


class KeyLocks:
    """One lock per key, held while a transaction for that key is in
    flight. Keys are always taken in sorted order, so two messages that
    share keys cannot wait on each other in a cycle."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @contextmanager
    def hold(self, keys: list[str]) -> Iterator[None]:
        taken: list[threading.Lock] = []
        try:
            for key in sorted(set(keys)):
                lock = self._lock(key)
                lock.acquire()
                taken.append(lock)
            yield
        finally:
            for lock in reversed(taken):
                lock.release()


# -- the handler -----------------------------------------------------------------------


def _log_type(hospital: HospitalConfig | None, msg_type: str) -> str:
    if hospital is not None and msg_type in hospital.expected_message_types:
        return msg_type
    return "(unlisted)"


class DirectHandler:
    def __init__(
        self,
        registry: HospitalRegistry,
        client: HapiClient,
        scope: RunScope,
        direct_log: DirectLog,
        *,
        clock: Clock | None = None,
        sending_app: str = "HL7BRIDGE",
        sending_facility: str = "HUB",
    ) -> None:
        self.registry = registry
        self.client = client
        self.scope = scope
        self.log = direct_log
        self.clock: Clock = clock if clock is not None else partial(datetime.now, UTC)
        self.sending_app = sending_app
        self.sending_facility = sending_facility
        self.locks = KeyLocks()
        self._ack_seq = itertools.count(1)
        self._ack_guard = threading.Lock()

    def _next_ack_id(self) -> str:
        with self._ack_guard:
            return f"BRIDGE{next(self._ack_seq):06d}"

    def _ack(
        self,
        decision: AckDecision,
        now: datetime,
        *,
        control_id: str = "",
        reply_app: str = "",
        reply_facility: str = "",
        trigger: str = "",
        version: str = "2.3",
    ) -> str:
        return build_ack(
            decision=decision,
            original_control_id=control_id or "UNKNOWN",
            ack_control_id=self._next_ack_id(),
            sending_app=self.sending_app,
            sending_facility=self.sending_facility,
            receiving_app=reply_app or "UNKNOWN",
            receiving_facility=reply_facility or "UNKNOWN",
            trigger_event=trigger,
            version=version,
            now=now,
        )

    def _finish(
        self,
        outcome: Outcome,
        *,
        raw: bytes,
        digest: str,
        received: str,
        hospital: HospitalConfig | None,
        identity: Identity | None,
        msg_type: str,
    ) -> AckDecision:
        decision = ack_code_for(outcome)
        row_id = self.log.record(
            run_id=self.scope.run_id,
            identity=identity,
            message_type=msg_type
            if hospital and msg_type in hospital.expected_message_types
            else "",
            digest=digest,
            received=received,
            decision=decision,
            raw=raw,
        )
        level = logging.INFO if decision.code == "AA" else logging.WARNING
        log.log(
            level,
            "direct message log_id=%d hospital=%s type=%s outcome=%s ack=%s",
            row_id,
            hospital.hospital_id if hospital else "-",
            _log_type(hospital, msg_type),
            outcome.reason,
            decision.code,
        )
        return decision

    def handle_bytes(self, raw: bytes) -> str:
        """Process one frame and return the ACK. Blocking: the FHIR calls
        run here, so the server calls it in a worker thread."""
        now = self.clock()
        received = utc_iso(now)
        digest = hashlib.sha256(raw).hexdigest()

        def early(outcome: Outcome, **kw: str) -> str:
            decision = self._finish(
                outcome,
                raw=raw,
                digest=digest,
                received=received,
                hospital=None,
                identity=None,
                msg_type="",
            )
            return self._ack(decision, now, **kw)

        try:
            text = raw.decode("utf-8")
            message = parse_hl7(text)
        except (UnicodeDecodeError, HL7ParseError):
            return early(Outcome("parse_error", "payload is not parseable as HL7 v2"))
        digest = body_hash(text)
        control_id = get_control_id(message)
        msg_type = get_message_type(message)
        msh4, msh6 = get_facilities(message)
        trigger = get_field(message, "MSH-9", component=2)
        reply = {
            "control_id": control_id,
            "reply_app": get_field(message, "MSH-3"),
            "reply_facility": msh4,
            "trigger": trigger if len(trigger) == 3 and trigger.isalnum() else "",
            "version": get_version(message),
        }
        if not control_id:
            return early(Outcome("missing_control_id", "MSH-10"), **reply)
        hospital = self.registry.match_facility(msh4, msh6)
        if hospital is None:
            return early(Outcome("unknown_hospital", "MSH-4/MSH-6"), **reply)
        identity = Identity(hospital.hospital_id, reply["reply_app"], msh4, control_id)

        def finish(outcome: Outcome) -> str:
            decision = self._finish(
                outcome,
                raw=raw,
                digest=digest,
                received=received,
                hospital=hospital,
                identity=identity,
                msg_type=msg_type,
            )
            return self._ack(decision, now, **reply)

        if msg_type not in hospital.expected_message_types:
            return finish(Outcome("unexpected_message_type", "MSH-9"))
        if msg_type in PATIENT_WRITING_TYPES:
            return finish(Outcome("patient_read_only", "MSH-9"))
        if msg_type not in DIRECT_TYPES:
            return finish(Outcome("unsupported_message_type", "MSH-9"))
        if get_field(message, "MSH-11", component=1) not in hospital.fhir.allowed_processing_ids:
            return finish(Outcome("refused_processing_id", "MSH-11"))

        with self.locks.hold([identity.key(self.scope.run_id)]):
            seen = self.log.committed_hash(self.scope.run_id, identity)
            if seen is not None:
                if seen == digest:
                    return finish(Outcome(DUPLICATE, committed=True))
                return finish(Outcome("control_id_conflict", "MSH-10"))
            return finish(self._write(text, hospital))

    def _write(self, text: str, hospital: HospitalConfig) -> Outcome:
        try:
            mapped = message_to_bundle(text, self.registry)
        except MappingError as exc:
            return outcome_for_error(exc)
        patient_entry = (mapped.bundle.entry or [])[0].resource
        mrn = str(patient_entry.identifier[0].value)  # type: ignore[union-attr]
        system = hospital.fhir.mrn_system(mapped.mrn_authority, hospital.systems.mrn)
        try:
            match = resolve_patient(self.client, system, mrn)
        except FhirPostError as exc:
            return outcome_for_error(exc)
        if match.outcome != "one" or match.patient_id is None:
            return Outcome(f"{match.outcome}_patient", "PID-3")
        body, scoped = scope_body(mapped, self.scope, match.patient_id)
        if not body["entry"]:
            return Outcome("nothing_to_write", "PV1-19")
        try:
            with self.locks.hold(lock_keys(body)):
                post_run(self.client, scoped, body, self.scope)
        except (
            FhirPostError,
            OutsideRunError,
            AmbiguousResourceError,
            StaleResultError,
            MappingError,
        ) as exc:
            return outcome_for_error(exc)
        return Outcome(COMMITTED, committed=True)

    async def handle_frame(self, payload: bytes) -> bytes:
        """MLLP server adapter: the blocking work runs in a worker thread,
        so one slow FHIR call does not stall other connections. The one
        blind-except boundary of this mode, as in the store pipeline."""
        try:
            return (await asyncio.to_thread(self.handle_bytes, payload)).encode("utf-8")
        except Exception as exc:  # noqa: BLE001  server boundary, logged below
            error_type = type(exc).__name__
            frames = "".join(traceback.format_tb(exc.__traceback__)).rstrip()
        return self._internal_error(payload, error_type, frames)

    def _internal_error(self, payload: bytes, error_type: str, frames: str) -> bytes:
        try:
            now = self.clock()
        except Exception:  # noqa: BLE001  server boundary
            now = datetime.now(UTC)
        decision = ack_code_for(Outcome("internal_error", "see log"))
        row = "none"
        try:
            row = str(
                self.log.record(
                    run_id=self.scope.run_id,
                    identity=None,
                    message_type="",
                    digest=hashlib.sha256(payload).hexdigest(),
                    received=utc_iso(now),
                    decision=decision,
                    raw=payload,
                )
            )
        except Exception:  # noqa: BLE001  server boundary
            log.error("direct log write failed; the frame is not logged, AR sent")
        log.error(
            "internal error handling frame log_id=%s bytes=%d error=%s\n%s",
            row,
            len(payload),
            error_type,
            frames,
        )
        return self._ack(decision, now).encode("utf-8")

    async def handle_junk(self, data: bytes) -> None:
        now = self.clock()
        self.log.record(
            run_id=self.scope.run_id,
            identity=None,
            message_type="",
            digest=hashlib.sha256(data).hexdigest(),
            received=utc_iso(now),
            decision=AckDecision("AR", "mllp_junk"),
            raw=data,
        )
        log.warning("mllp junk bytes logged bytes=%d", len(data))


def check_profiles_for_direct(registry: HospitalRegistry) -> None:
    """Direct mode only matches patients. Every loaded profile must say
    ``patient_policy: match_only``; ConfigError names the first that does not."""
    from ..errors import ConfigError

    for hospital in registry.by_id.values():
        if hospital.fhir.patient_policy != "match_only":
            raise ConfigError(
                f"profile {hospital.hospital_id} has patient_policy"
                f" {hospital.fhir.patient_policy}; listen --target fhir needs match_only"
                " in every profile it loads.",
                hint="Pass --config with a folder of match_only profiles, such as the"
                " bundled example (hl7bridge profile show --config"
                " <package>/profiles/examples).",
            )


__all__ = [
    "AE_REASONS",
    "AR_REASONS",
    "DIRECT_TYPES",
    "DirectHandler",
    "DirectLog",
    "KeyLocks",
    "Outcome",
    "ack_code_for",
    "body_hash",
    "check_profiles_for_direct",
    "outcome_for_error",
]
