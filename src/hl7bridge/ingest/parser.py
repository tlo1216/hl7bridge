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
from ..logsafety import install_third_party_redaction

# python-hl7 logs a bad escape or a stray segment with the message text;
# its records are withheld wherever this parser is used.
install_third_party_redaction()

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


def raw_field(message: hl7.Message, ref: str, *, segment_num: int = 1) -> str:
    """The field as sent, still escaped, with its repetitions; '' when absent."""
    segment_name, field_num = split_field_ref(ref)
    try:
        segment = message.segments(segment_name)[segment_num - 1]
    except (KeyError, IndexError):
        return ""
    if field_num >= len(segment):
        return ""
    return str(segment[field_num])


def get_repetition_count(message: hl7.Message, ref: str, *, segment_num: int = 1) -> int:
    """How many repetitions the field holds (0 when it is empty or absent)."""
    text = raw_field(message, ref, segment_num=segment_num)
    return len(text.split(message.separators[2])) if text else 0


# The escape sequences python-hl7 decodes without complaint (HL7 v2
# chapter 2.7): separators, highlighting, formatting with an optional
# count, and hex data in whole bytes. \Z..\ (local), \C..\ and \M..\
# (character sets) are not decoded, so they count as malformed here.
_ESCAPE_OK = re.compile(r"[FRSTEHN]|\.(?:br|sp|in|ti|sk|ce|fi|nf)\d*|X(?:[0-9A-Fa-f]{2})+")


def has_malformed_escape(raw: str, esc: str) -> bool:
    """True when ``raw`` (a field as sent) holds an escape sequence that is
    unterminated, empty or one python-hl7 cannot decode. python-hl7 logs
    those with the whole field value, so callers refuse them first."""
    if not esc or esc not in raw:
        return False
    parts = raw.split(esc)
    if len(parts) % 2 == 0:  # an escape character with no closing one
        return True
    return any(not _ESCAPE_OK.fullmatch(p) for p in parts[1::2])


def pick_mrn_repetition(types: list[str]) -> int | None:
    """Which PID-3 repetition holds the medical record number.

    ``types`` is PID-3 component 5 (identifier type code) of each
    repetition, in order. With no type code anywhere, the first repetition
    is the MRN, as before. When any repetition carries a type code, the
    first one typed MR is the MRN, and None (refuse) when none is: a typed
    list without MR names no medical record number to key a patient on.
    """
    if not types:
        return None
    if not any(types):
        return 1
    for n, code in enumerate(types, start=1):
        if code == "MR":
            return n
    return None


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


def get_mrn(message: hl7.Message) -> str | None:
    """PID-3 component 1 of the repetition ``pick_mrn_repetition`` chooses:
    '' when PID-3 is empty, None when PID-3 is typed and holds no MR."""
    reps = get_repetition_count(message, "PID-3")
    if reps == 0:
        return ""
    types = [get_field(message, "PID-3", repetition=n, component=5) for n in range(1, reps + 1)]
    chosen = pick_mrn_repetition(types)
    if chosen is None:
        return None
    return get_field(message, "PID-3", repetition=chosen, component=1)


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
