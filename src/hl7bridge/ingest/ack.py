"""ACK construction. Every inbound message gets an explicit AA / AE / AR
decision; this module renders that decision as an
HL7 ACK message.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..hl7time import format_hl7_ts

AckCode = Literal["AA", "AE", "AR"]

# The decision table lives in pipeline.py; these are its stable reason codes.
REASON_PARSE_ERROR = "parse_error"
REASON_MLLP_JUNK = "mllp_junk"
REASON_UNKNOWN_HOSPITAL = "unknown_hospital"
REASON_UNEXPECTED_TYPE = "unexpected_message_type"
REASON_MISSING_CONTROL_ID = "missing_control_id"
REASON_MISSING_MRN = "missing_mrn"
REASON_MISSING_MERGE_MRN = "missing_merge_mrn"
REASON_MISSING_ACCESSION = "missing_accession"
REASON_BAD_TIMESTAMP = "bad_timestamp"
REASON_UNKNOWN_REPORT_STATUS = "unknown_report_status"
REASON_UNSUPPORTED_ORDER_CONTROL = "unsupported_order_control"
REASON_NO_HANDLER = "no_handler_for_type"
REASON_MISSING_MSA = "missing_msa"
REASON_INTERNAL_ERROR = "internal_error"
ALL_REASONS = frozenset(
    {
        REASON_PARSE_ERROR,
        REASON_MLLP_JUNK,
        REASON_UNKNOWN_HOSPITAL,
        REASON_UNEXPECTED_TYPE,
        REASON_MISSING_CONTROL_ID,
        REASON_MISSING_MRN,
        REASON_MISSING_MERGE_MRN,
        REASON_MISSING_ACCESSION,
        REASON_BAD_TIMESTAMP,
        REASON_UNKNOWN_REPORT_STATUS,
        REASON_UNSUPPORTED_ORDER_CONTROL,
        REASON_NO_HANDLER,
        REASON_MISSING_MSA,
        REASON_INTERNAL_ERROR,
    }
)


_ACK_ESCAPES = (("\\", "\\E\\"), ("|", "\\F\\"), ("^", "\\S\\"), ("&", "\\T\\"), ("~", "\\R\\"))


def ack_value(value: str) -> str:
    r"""One value echoed into an ACK as plain HL7 text: the separators
    escaped and every control character (CR, LF, the MLLP framing bytes)
    dropped. The parser hands back decoded values, so a sender's ``\X0D\``
    or ``\F\`` in MSH-3 arrives here as a real CR or ``|``; echoed raw, it
    could start a second MSA or shift MSA-2."""
    text = "".join(c for c in value if unicodedata.category(c) != "Cc")
    for raw, escaped in _ACK_ESCAPES:
        text = text.replace(raw, escaped)
    return text


@dataclass(frozen=True, slots=True)
class AckDecision:
    """``reason`` is 'accepted' or a REASON_* code. ``detail`` can quote the
    rejected value and goes to the quarantine row only. ``ack_text`` is
    fixed text from the code (usually a field name such as ``PID-7``) that
    may go back to the sender; it never holds a sender-written value."""

    code: AckCode
    reason: str
    detail: str = ""
    ack_text: str = ""


def build_ack(
    *,
    decision: AckDecision,
    original_control_id: str,
    ack_control_id: str,
    sending_app: str,
    sending_facility: str,
    receiving_app: str,
    receiving_facility: str,
    trigger_event: str,
    version: str,
    now: datetime,
) -> str:
    """Render an original-mode ACK. For unparseable input the caller passes
    original_control_id='UNKNOWN' so the rejection is still addressable.
    Every value is passed through ``ack_value``, so nothing a sender wrote
    can add a segment or a field to the ACK."""
    ts = format_hl7_ts(now, include_offset=True)
    # MSA-3 is the reason code and the fixed ack_text, never the detail.
    msa_text = f"{decision.reason}: {decision.ack_text}" if decision.ack_text else decision.reason
    ack_type = f"ACK^{trigger_event}" if trigger_event else "ACK"
    msh = "|".join(
        [
            "MSH",
            "^~\\&",
            ack_value(sending_app),
            ack_value(sending_facility),
            ack_value(receiving_app),
            ack_value(receiving_facility),
            ts,
            "",
            ack_value(ack_type),
            ack_value(ack_control_id),
            "P",
            ack_value(version),
        ]
    )
    msa_fields = ["MSA", decision.code, ack_value(original_control_id) or "UNKNOWN"]
    if decision.code != "AA":
        msa_fields.append(ack_value(msa_text))
    return msh + "\r" + "|".join(msa_fields) + "\r"
