"""Thin wrapper over python-hl7 with explicit field addressing.

Field references use HL7 spec positions ("OBR-3", "PID-5"). python-hl7
special-cases MSH so that spec positions work there too (MSH-9 is the
message type, MSH-10 the control id); verified by unit test.

``extract_field`` already unescapes HL7 escape sequences (\\T\\ -> &), so
values returned here are plain text and must NOT be unescaped again.
"""

from __future__ import annotations

import re

import hl7

from ..errors import HL7ParseError

_FIELD_REF_RE = re.compile(r"^([A-Z][A-Z0-9]{2})-(\d{1,2})$")


def normalize_segments(raw: str) -> str:
    """Normalize segment terminators to the HL7-standard carriage return."""
    return raw.replace("\r\n", "\r").replace("\n", "\r").strip("\r") + "\r"


def parse_hl7(raw: str) -> hl7.Message:
    normalized = normalize_segments(raw)
    if not normalized.strip():
        raise HL7ParseError("empty message")
    try:
        return hl7.parse(normalized)
    except (hl7.ParseException, ValueError, IndexError) as exc:
        raise HL7ParseError(f"not parseable as HL7 v2: {exc}") from exc


def split_field_ref(ref: str) -> tuple[str, int]:
    m = _FIELD_REF_RE.match(ref)
    if m is None:
        raise ValueError(f"bad HL7 field reference {ref!r}; expected like 'OBR-3'")
    return m.group(1), int(m.group(2))


def get_field(
    message: hl7.Message,
    ref: str,
    *,
    repetition: int = 1,
    component: int = 1,
    segment_num: int = 1,
) -> str:
    """Return the (unescaped) component value, or '' when absent.

    Absence is a normal outcome; callers that require the field raise
    MessageValidationError themselves with a reason code.
    """
    segment_name, field_num = split_field_ref(ref)
    try:
        value = message.extract_field(
            segment_name,
            segment_num=segment_num,
            field_num=field_num,
            repeat_num=repetition,
            component_num=component,
        )
    except (KeyError, IndexError):
        return ""
    return str(value).strip()


def get_segment_count(message: hl7.Message, segment_name: str) -> int:
    try:
        return len(message.segments(segment_name))
    except KeyError:
        return 0


def get_message_type(message: hl7.Message) -> str:
    """Return 'TYPE^TRIGGER' (e.g. 'ORU^R01'), or 'ACK' for bare ACKs."""
    msg_type = get_field(message, "MSH-9", component=1)
    trigger = get_field(message, "MSH-9", component=2)
    if not msg_type:
        return ""
    return f"{msg_type}^{trigger}" if trigger else msg_type


def get_control_id(message: hl7.Message) -> str:
    return get_field(message, "MSH-10")


def get_facilities(message: hl7.Message) -> tuple[str, str]:
    """(sending facility MSH-4, receiving facility MSH-6)."""
    return get_field(message, "MSH-4"), get_field(message, "MSH-6")


def get_version(message: hl7.Message) -> str:
    return get_field(message, "MSH-12") or "2.3"


def collect_obx_text(message: hl7.Message) -> str:
    """Join the text of all TX/FT/ST OBX segments, one line per segment."""
    lines: list[str] = []
    for i in range(1, get_segment_count(message, "OBX") + 1):
        value_type = get_field(message, "OBX-2", segment_num=i)
        if value_type in ("TX", "FT", "ST", ""):
            text = get_field(message, "OBX-5", segment_num=i)
            if text:
                lines.append(text)
    return "\n".join(lines)
