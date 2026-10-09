"""ACK construction. Every inbound message gets an explicit AA / AE / AR
decision (engineering rule 2); this module renders that decision as an
HL7 ACK message.
"""

from __future__ import annotations

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
REASON_MISSING_MSA = "missing_msa"
REASON_INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class AckDecision:
    code: AckCode
    reason: str  # 'accepted' or a REASON_* quarantine code
    detail: str = ""


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
    original_control_id='UNKNOWN' so the rejection is still addressable."""
    ts = format_hl7_ts(now, include_offset=True)
    msa_text = decision.detail or decision.reason
    ack_type = f"ACK^{trigger_event}" if trigger_event else "ACK"
    msh = "|".join(
        [
            "MSH",
            "^~\\&",
            sending_app,
            sending_facility,
            receiving_app,
            receiving_facility,
            ts,
            "",
            ack_type,
            ack_control_id,
            "P",
            version,
        ]
    )
    msa_fields = ["MSA", decision.code, original_control_id or "UNKNOWN"]
    if decision.code != "AA":
        msa_fields.append(msa_text)
    return msh + "\r" + "|".join(msa_fields) + "\r"
