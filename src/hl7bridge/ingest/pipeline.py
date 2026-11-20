"""Ingest pipeline: bytes in, explicit ACK decision out.

ACK decision table (one explicit ACK per message; tested row by row):

| Condition                                        | ACK | Destination              |
|--------------------------------------------------|-----|--------------------------|
| Payload not valid UTF-8                          | AR  | quarantine parse_error   |
| Not parseable as HL7 (no MSH, garbage)           | AR  | quarantine parse_error   |
| MSH-10 control id missing                        | AR  | quarantine missing_control_id |
| MSH-4/MSH-6 match no configured hospital         | AR  | quarantine unknown_hospital |
| Message type not expected for the hospital       | AR  | quarantine unexpected_message_type |
| Type-specific validation failed (see REASON_*)   | AE  | quarantine <reason>      |
| Expected type but no handler (config/code bug)   | AE  | quarantine no_handler_for_type |
| Internal error (server boundary)                 | AR  | quarantine internal_error |
| Valid                                            | AA  | messages + domain tables |
| Valid, duplicate (hospital_id, control_id)       | AA  | duplicate_count bump only |

MLLP junk bytes get no ACK (nothing addressable) but are quarantined as
mllp_junk. ACK messages from hospitals are accepted for any configured
hospital regardless of expected_message_types: they are transport plumbing
for ACK tracking, not clinical traffic.
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from ..config import HospitalConfig, HospitalRegistry
from ..errors import HL7ParseError, MessageValidationError, TimestampError
from ..hl7time import parse_hl7_ts
from ..logsafety import short_hash
from ..store import utc_iso
from ..store.repo import (
    AckInRow,
    OrderRow,
    PatientRow,
    ReportEventRow,
    ReportRow,
    insert_ack_in,
    insert_quarantine,
    insert_report_event,
    message_row_id,
    record_merge,
    record_message,
    upsert_order,
    upsert_patient,
    upsert_report,
)
from ..store.repo import (
    cancel_order as repo_cancel_order,
)
from . import ack as ackmod
from .ack import AckDecision, build_ack
from .parser import (
    collect_obx_text,
    get_control_id,
    get_facilities,
    get_field,
    get_message_type,
    get_version,
    parse_hl7,
)

if TYPE_CHECKING:
    import hl7

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

VALID_REPORT_STATUSES = ("P", "F", "C")
VALID_ACK_CODES = ("AA", "AE", "AR")
SUPPORTED_ORDER_CONTROLS = ("NW", "CA")


_PARSE_ACK_TEXT = {
    "not_utf8": "payload is not valid UTF-8",
    "not_hl7": "payload is not parseable as HL7 v2",
}


def _log_type(hospital: HospitalConfig, msg_type: str) -> str:
    """MSH-9 is sender-written. Log it only when it is one of the types the
    hospital profile lists; any other ACK variant is logged as plain ACK."""
    if msg_type in hospital.expected_message_types or msg_type == "ACK":
        return msg_type
    return "ACK" if msg_type.startswith("ACK^") else "(unlisted)"


class IngestPipeline:
    """Parses, validates, stores, and ACKs one message at a time.

    PHI discipline: log statements in this class carry only store row ids,
    a short hash of MSH-10, hospital ids, message types from configuration,
    byte lengths and reason codes. MSH-10 itself is written by the sender and
    can hold anything, so it is never logged; the raw value stays in the
    database and in reports. Patient names, MRNs and report text go to the
    database, never to the log.
    """

    def __init__(
        self,
        registry: HospitalRegistry,
        conn: sqlite3.Connection,
        *,
        clock: Clock | None = None,
        sending_app: str = "HL7BRIDGE",
        sending_facility: str = "HUB",
    ) -> None:
        self.registry = registry
        self.conn = conn
        self.clock: Clock = clock if clock is not None else partial(datetime.now, UTC)
        self.sending_app = sending_app
        self.sending_facility = sending_facility
        self._ack_seq = itertools.count(1)

    # -- public entry points ------------------------------------------------

    def handle_bytes(self, raw: bytes) -> str:
        """Process one MLLP payload and return the ACK message to send."""
        now = self.clock()
        received = utc_iso(now)

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return self._reject_unparseable(
                raw, "not_utf8", f"payload is not valid UTF-8: {exc}", received, now
            )

        try:
            message = parse_hl7(text)
        except HL7ParseError as exc:
            return self._reject_unparseable(raw, "not_hl7", str(exc), received, now)

        control_id = get_control_id(message)
        msg_type = get_message_type(message)
        msh4, msh6 = get_facilities(message)
        version = get_version(message)
        trigger = get_field(message, "MSH-9", component=2)
        reply_app = get_field(message, "MSH-3")
        reply_facility = msh4

        def respond(decision: AckDecision) -> str:
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

        if not control_id:
            decision = AckDecision("AR", ackmod.REASON_MISSING_CONTROL_ID, "MSH-10 is empty")
            self._quarantine(None, decision, raw, received)
            return respond(decision)

        hospital = self.registry.match_facility(msh4, msh6)
        if hospital is None:
            decision = AckDecision(
                "AR",
                ackmod.REASON_UNKNOWN_HOSPITAL,
                f"MSH-4={msh4!r} MSH-6={msh6!r} match no configured hospital",
            )
            self._quarantine(None, decision, raw, received)
            return respond(decision)

        is_ack = msg_type == "ACK" or msg_type.startswith("ACK^")
        if not is_ack and msg_type not in hospital.expected_message_types:
            decision = AckDecision(
                "AR",
                ackmod.REASON_UNEXPECTED_TYPE,
                f"{msg_type!r} not in expected types for {hospital.hospital_id}",
            )
            self._quarantine(hospital.hospital_id, decision, raw, received)
            return respond(decision)

        try:
            with self.conn:
                outcome = record_message(
                    self.conn,
                    hospital_id=hospital.hospital_id,
                    control_id=control_id,
                    message_type=msg_type,
                    received_at_utc=received,
                    raw=text,
                )
                if outcome == "new":
                    self._dispatch(hospital, message, msg_type, control_id, received)
        except MessageValidationError as exc:
            # The transaction above rolled back: no partial domain writes.
            decision = AckDecision("AE", exc.reason, exc.detail)
            quarantine_id = self._quarantine(hospital.hospital_id, decision, raw, received)
            log.warning(
                "message rejected hospital=%s quarantine_id=%d ctl_hash=%s type=%s reason=%s",
                hospital.hospital_id,
                quarantine_id,
                short_hash(control_id),
                _log_type(hospital, msg_type),
                exc.reason,
            )
            return respond(decision)

        row_id = message_row_id(self.conn, hospital_id=hospital.hospital_id, control_id=control_id)
        log.info(
            "%s hospital=%s message_id=%d ctl_hash=%s type=%s",
            "duplicate message re-acked" if outcome == "duplicate" else "message accepted",
            hospital.hospital_id,
            row_id,
            short_hash(control_id),
            _log_type(hospital, msg_type),
        )
        return respond(AckDecision("AA", "accepted"))

    async def handle_frame(self, payload: bytes) -> bytes:
        """MLLP server adapter. This is the one blind-except boundary in the
        codebase: the listener must survive a poison message, but the failure
        is loud (stack trace logged, message quarantined, AR returned)."""
        try:
            return self.handle_bytes(payload).encode("utf-8")
        except Exception as exc:  # noqa: BLE001  server boundary, logged below
            now = self.clock()
            decision = AckDecision("AR", ackmod.REASON_INTERNAL_ERROR, "unhandled error; see log")
            quarantine_id = self._quarantine(None, decision, payload, utc_iso(now))
            # Exception type and stack frames only. The exception message can
            # quote field values, so it is not logged; the frames show code.
            log.error(
                "internal error handling frame quarantine_id=%d bytes=%d error=%s\n%s",
                quarantine_id,
                len(payload),
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)).rstrip(),
            )
            return build_ack(
                decision=decision,
                original_control_id="UNKNOWN",
                ack_control_id=self._next_ack_id(),
                sending_app=self.sending_app,
                sending_facility=self.sending_facility,
                receiving_app="UNKNOWN",
                receiving_facility="UNKNOWN",
                trigger_event="",
                version="2.3",
                now=now,
            ).encode("utf-8")

    async def handle_junk(self, data: bytes) -> None:
        """Quarantine bytes received outside MLLP framing. No ACK possible."""
        decision = AckDecision("AR", ackmod.REASON_MLLP_JUNK, f"{len(data)} bytes outside framing")
        self._quarantine(None, decision, data, utc_iso(self.clock()))

    # -- internals -----------------------------------------------------------

    def _next_ack_id(self) -> str:
        return f"BRIDGE{next(self._ack_seq):06d}"

    def _reject_unparseable(
        self, raw: bytes, cause: str, detail: str, received: str, now: datetime
    ) -> str:
        """``cause`` is a fixed code for the log and the ACK. ``detail`` is the
        parser's own text, which can quote the broken segment, so it goes to
        the quarantine row only."""
        quarantine_id = self._quarantine(
            None, AckDecision("AR", ackmod.REASON_PARSE_ERROR, detail), raw, received
        )
        log.warning(
            "unparseable message quarantined quarantine_id=%d cause=%s bytes=%d",
            quarantine_id,
            cause,
            len(raw),
        )
        return build_ack(
            decision=AckDecision("AR", ackmod.REASON_PARSE_ERROR, _PARSE_ACK_TEXT[cause]),
            original_control_id="UNKNOWN",
            ack_control_id=self._next_ack_id(),
            sending_app=self.sending_app,
            sending_facility=self.sending_facility,
            receiving_app="UNKNOWN",
            receiving_facility="UNKNOWN",
            trigger_event="",
            version="2.3",
            now=now,
        )

    def _quarantine(
        self, hospital_id: str | None, decision: AckDecision, raw: bytes, received: str
    ) -> int:
        assert decision.code in ("AE", "AR")
        with self.conn:
            return insert_quarantine(
                self.conn,
                hospital_id=hospital_id,
                reason=decision.reason,
                detail=decision.detail,
                raw=raw,
                ack_code=decision.code,
                received_at_utc=received,
            )

    def _parse_ts(self, hospital: HospitalConfig, value: str, field: str) -> str:
        try:
            return utc_iso(parse_hl7_ts(value, default_tz=hospital.tzinfo))
        except TimestampError as exc:
            raise MessageValidationError(ackmod.REASON_BAD_TIMESTAMP, f"{field}: {exc}") from exc

    def _dispatch(
        self,
        hospital: HospitalConfig,
        message: hl7.Message,
        msg_type: str,
        control_id: str,
        received: str,
    ) -> None:
        if msg_type == "ACK" or msg_type.startswith("ACK^"):
            self._handle_ack(hospital, message, control_id, received)
        elif msg_type.startswith("ADT^"):
            self._handle_adt(hospital, message, msg_type, received)
        elif msg_type == "ORM^O01":
            self._handle_orm(hospital, message, received)
        elif msg_type == "ORU^R01":
            self._handle_oru(hospital, message, control_id, received)
        else:
            # expected_message_types allowed a type we have no handler for:
            # that is a config/code mismatch and must be loud, not ignored.
            raise MessageValidationError(
                ackmod.REASON_NO_HANDLER,
                f"no handler implemented for expected type {msg_type!r}",
            )

    def _extract_patient(self, hospital: HospitalConfig, message: hl7.Message) -> PatientRow:
        mrn = get_field(message, "PID-3", repetition=1, component=1)
        if not mrn:
            raise MessageValidationError(ackmod.REASON_MISSING_MRN, "PID-3 is empty")
        birth_raw = get_field(message, "PID-7")
        birth_date: str | None = None
        if birth_raw:
            try:
                parsed = parse_hl7_ts(birth_raw, default_tz=hospital.tzinfo)
            except TimestampError as exc:
                raise MessageValidationError(ackmod.REASON_BAD_TIMESTAMP, f"PID-7: {exc}") from exc
            birth_date = parsed.date().isoformat()
        return PatientRow(
            hospital_id=hospital.hospital_id,
            mrn=mrn,
            family_name=get_field(message, "PID-5", component=1),
            given_name=get_field(message, "PID-5", component=2),
            birth_date=birth_date,
            sex=get_field(message, "PID-8") or None,
        )

    def _handle_adt(
        self, hospital: HospitalConfig, message: hl7.Message, msg_type: str, received: str
    ) -> None:
        patient = self._extract_patient(hospital, message)
        upsert_patient(self.conn, patient)
        if msg_type == "ADT^A40":
            merged_mrn = get_field(message, "MRG-1", repetition=1, component=1)
            if not merged_mrn:
                raise MessageValidationError(
                    ackmod.REASON_MISSING_MERGE_MRN, "ADT^A40 without MRG-1"
                )
            event_raw = get_field(message, "MSH-7")
            merged_at = self._parse_ts(hospital, event_raw, "MSH-7") if event_raw else received
            record_merge(
                self.conn,
                hospital_id=hospital.hospital_id,
                surviving_mrn=patient.mrn,
                merged_mrn=merged_mrn,
                merged_at_utc=merged_at,
            )

    def _accession(self, hospital: HospitalConfig, message: hl7.Message) -> str:
        accession = get_field(message, hospital.accession_field)
        if not accession:
            raise MessageValidationError(
                ackmod.REASON_MISSING_ACCESSION,
                f"{hospital.accession_field} is empty (per {hospital.hospital_id} config)",
            )
        return accession

    def _handle_orm(self, hospital: HospitalConfig, message: hl7.Message, received: str) -> None:
        order_control = get_field(message, "ORC-1")
        if order_control not in SUPPORTED_ORDER_CONTROLS:
            raise MessageValidationError(
                ackmod.REASON_UNSUPPORTED_ORDER_CONTROL, f"ORC-1={order_control!r}"
            )
        accession = self._accession(hospital, message)
        patient = self._extract_patient(hospital, message)
        upsert_patient(self.conn, patient)

        ordered_raw = get_field(message, "ORC-9") or get_field(message, "MSH-7")
        if not ordered_raw:
            raise MessageValidationError(
                ackmod.REASON_BAD_TIMESTAMP, "no order timestamp in ORC-9 or MSH-7"
            )
        ordered_at = self._parse_ts(hospital, ordered_raw, "ORC-9/MSH-7")

        if order_control == "CA":
            if (
                repo_cancel_order(self.conn, hospital_id=hospital.hospital_id, accession=accession)
                == "not_found"
            ):
                # Cancellation for an order we never saw: keep the tombstone
                # so reconciliation can reason about it.
                upsert_order(
                    self.conn,
                    OrderRow(
                        hospital_id=hospital.hospital_id,
                        accession=accession,
                        placer_order_id=get_field(message, "ORC-2") or None,
                        procedure_code=get_field(message, "OBR-4", component=1) or None,
                        procedure_description=get_field(message, "OBR-4", component=2) or None,
                        code_system=hospital.procedure_code_system,
                        modality=get_field(message, "OBR-24") or None,
                        priority=get_field(message, "OBR-5") or None,
                        patient_mrn=patient.mrn,
                        ordered_at_utc=ordered_at,
                        status="cancelled",
                    ),
                )
            return

        upsert_order(
            self.conn,
            OrderRow(
                hospital_id=hospital.hospital_id,
                accession=accession,
                placer_order_id=get_field(message, "ORC-2") or None,
                procedure_code=get_field(message, "OBR-4", component=1) or None,
                procedure_description=get_field(message, "OBR-4", component=2) or None,
                code_system=hospital.procedure_code_system,
                modality=get_field(message, "OBR-24") or None,
                priority=get_field(message, "OBR-5") or None,
                patient_mrn=patient.mrn,
                ordered_at_utc=ordered_at,
                status="new",
            ),
        )

    def _handle_oru(
        self, hospital: HospitalConfig, message: hl7.Message, control_id: str, received: str
    ) -> None:
        accession = self._accession(hospital, message)
        status = get_field(message, "OBR-25")
        if status not in VALID_REPORT_STATUSES:
            raise MessageValidationError(
                ackmod.REASON_UNKNOWN_REPORT_STATUS,
                f"OBR-25={status!r} not one of {VALID_REPORT_STATUSES}; refusing to guess",
            )
        patient = self._extract_patient(hospital, message)
        upsert_patient(self.conn, patient)

        result_raw = get_field(message, "OBR-22")
        result_at = self._parse_ts(hospital, result_raw, "OBR-22") if result_raw else None
        observed_raw = get_field(message, "OBR-7")
        observed_at = self._parse_ts(hospital, observed_raw, "OBR-7") if observed_raw else None

        insert_report_event(
            self.conn,
            ReportEventRow(
                hospital_id=hospital.hospital_id,
                accession=accession,
                control_id=control_id,
                status=status,  # type: ignore[arg-type]
                observed_at_utc=observed_at,
                result_at_utc=result_at,
                received_at_utc=received,
            ),
        )
        upsert_report(
            self.conn,
            ReportRow(
                hospital_id=hospital.hospital_id,
                accession=accession,
                control_id=control_id,
                status=status,  # type: ignore[arg-type]
                report_text=collect_obx_text(message),
                patient_mrn=patient.mrn,
                result_at_utc=result_at,
                received_at_utc=received,
            ),
        )

    def _handle_ack(
        self, hospital: HospitalConfig, message: hl7.Message, control_id: str, received: str
    ) -> None:
        msa_code = get_field(message, "MSA-1")
        msa_control = get_field(message, "MSA-2")
        if not msa_code or not msa_control:
            raise MessageValidationError(
                ackmod.REASON_MISSING_MSA, f"MSA-1={msa_code!r} MSA-2={msa_control!r}"
            )
        if msa_code not in VALID_ACK_CODES:
            raise MessageValidationError(
                ackmod.REASON_MISSING_MSA, f"MSA-1={msa_code!r} not one of {VALID_ACK_CODES}"
            )
        reason = get_field(message, "MSA-3") or get_field(message, "ERR-1") or None
        insert_ack_in(
            self.conn,
            AckInRow(
                hospital_id=hospital.hospital_id,
                ack_control_id=control_id,
                msa_code=msa_code,
                msa_control_id=msa_control,
                reason=reason,
                received_at_utc=received,
            ),
        )
