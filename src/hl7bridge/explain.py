"""``hl7bridge explain``: a message, one line per field, with names.

Each field prints as ``PID-5 Patient name: ZZZSAMPLE^ONE``, components are
split out under it when more than one is valued, repetitions get ``[n]``,
coded fields show their meaning (``OBR-25 Result status: F = final``) and
empty fields are hidden unless ``--all``. Field names are short labels in
plain words for the segments a radiology or lab interface uses; any other
position prints without a name. Unknown codes are shown as sent, with
``(not a known code)``; explain describes a message, it never maps one.

The output is the message's own content, so it holds whatever the message
holds (names, MRNs, report text). It goes to stdout only and is never
logged. Every value is cleaned of terminal control characters.

This module splits the message itself rather than going through the
parser, so a field shows exactly as it was sent, escapes decoded.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from .errors import HL7ParseError
from .logsafety import printable

VALUE_LIMIT = 2000

SEGMENTS: dict[str, str] = {
    "MSH": "message header",
    "EVN": "event type",
    "PID": "patient identification",
    "PV1": "patient visit",
    "NK1": "next of kin",
    "AL1": "allergy",
    "DG1": "diagnosis",
    "MRG": "merge patient information",
    "ORC": "common order",
    "OBR": "observation request",
    "OBX": "observation result",
    "NTE": "notes and comments",
    "SPM": "specimen",
    "MSA": "message acknowledgment",
    "ERR": "error",
}

FIELDS: dict[str, dict[int, str]] = {
    "MSH": {
        1: "Field separator",
        2: "Encoding characters",
        3: "Sending application",
        4: "Sending facility",
        5: "Receiving application",
        6: "Receiving facility",
        7: "Message date and time",
        8: "Security",
        9: "Message type",
        10: "Message control id",
        11: "Processing id",
        12: "Version",
        13: "Sequence number",
        15: "Accept acknowledgment type",
        16: "Application acknowledgment type",
        17: "Country code",
        18: "Character set",
    },
    "EVN": {1: "Event type code", 2: "Recorded date and time", 5: "Operator", 6: "Event occurred"},
    "PID": {
        1: "Set id",
        2: "Patient id (old)",
        3: "Patient identifier list",
        4: "Alternate patient id",
        5: "Patient name",
        6: "Mother's maiden name",
        7: "Date of birth",
        8: "Administrative sex",
        10: "Race",
        11: "Patient address",
        13: "Home phone",
        14: "Business phone",
        15: "Primary language",
        16: "Marital status",
        18: "Patient account number",
        19: "SSN (never send real ones)",
        22: "Ethnic group",
        29: "Date of death",
        30: "Death indicator",
    },
    "PV1": {
        1: "Set id",
        2: "Patient class",
        3: "Assigned location",
        4: "Admission type",
        7: "Attending doctor",
        8: "Referring doctor",
        10: "Hospital service",
        14: "Admit source",
        17: "Admitting doctor",
        18: "Patient type",
        19: "Visit number",
        20: "Financial class",
        36: "Discharge disposition",
        39: "Servicing facility",
        44: "Admit date and time",
        45: "Discharge date and time",
    },
    "NK1": {1: "Set id", 2: "Name", 3: "Relationship", 4: "Address", 5: "Phone"},
    "AL1": {1: "Set id", 2: "Allergen type", 3: "Allergen", 4: "Severity", 5: "Reaction"},
    "DG1": {1: "Set id", 3: "Diagnosis code", 4: "Diagnosis description", 6: "Diagnosis type"},
    "MRG": {1: "Prior patient identifier list", 3: "Prior account number", 5: "Prior visit number"},
    "ORC": {
        1: "Order control",
        2: "Placer order number",
        3: "Filler order number",
        4: "Placer group number",
        5: "Order status",
        7: "Quantity and timing",
        9: "Transaction date and time",
        10: "Entered by",
        12: "Ordering provider",
        15: "Order effective date and time",
    },
    "OBR": {
        1: "Set id",
        2: "Placer order number",
        3: "Filler order number",
        4: "Universal service id",
        5: "Priority (old)",
        6: "Requested date and time",
        7: "Observation date and time",
        8: "Observation end date and time",
        13: "Relevant clinical information",
        16: "Ordering provider",
        18: "Placer field 1",
        19: "Placer field 2",
        20: "Filler field 1",
        21: "Filler field 2",
        22: "Results report or status change time",
        24: "Diagnostic service section",
        25: "Result status",
        27: "Quantity and timing",
        31: "Reason for study",
        32: "Principal result interpreter",
    },
    "OBX": {
        1: "Set id",
        2: "Value type",
        3: "Observation identifier",
        4: "Observation sub-id",
        5: "Observation value",
        6: "Units",
        7: "Reference range",
        8: "Abnormal flags",
        11: "Observation result status",
        14: "Observation date and time",
        15: "Producer id",
        16: "Responsible observer",
        19: "Analysis date and time",
    },
    "NTE": {1: "Set id", 2: "Source of comment", 3: "Comment", 4: "Comment type"},
    "SPM": {
        1: "Set id",
        2: "Specimen id",
        4: "Specimen type",
        8: "Specimen source site",
        17: "Collection date and time",
        18: "Received date and time",
    },
    "MSA": {1: "Acknowledgment code", 2: "Message control id", 3: "Text message"},
    "ERR": {1: "Error code and location", 2: "Error location", 3: "HL7 error code", 4: "Severity"},
}

# Data types of the fields whose components get names.
_CE = (
    "identifier",
    "text",
    "coding system",
    "alternate identifier",
    "alternate text",
    "alternate coding system",
    "coding system version",
    "alternate version",
    "original text",
)
_CX = (
    "id number",
    "check digit",
    "check digit scheme",
    "assigning authority",
    "identifier type",
    "assigning facility",
)
_XPN = ("family name", "given name", "second given name or initial", "suffix", "prefix", "degree")
_XAD = (
    "street address",
    "other designation",
    "city",
    "state or province",
    "postal code",
    "country",
    "address type",
)
_XTN = (
    "number",
    "use code",
    "equipment type",
    "email address",
    "country code",
    "area code",
    "local number",
)
_XCN = ("id number", "family name", "given name", "second given name", "suffix", "prefix")
_HD = ("namespace id", "universal id", "universal id type")
_EI = ("entity id", "namespace id", "universal id", "universal id type")
_PL = ("point of care", "room", "bed", "facility", "location status")
_MSG = ("message code", "trigger event", "message structure")
COMPONENTS: dict[str, tuple[str, ...]] = {
    "MSH-3": _HD, "MSH-4": _HD, "MSH-5": _HD, "MSH-6": _HD, "MSH-9": _MSG,
    "PID-3": _CX, "PID-4": _CX, "PID-5": _XPN, "PID-6": _XPN, "PID-11": _XAD,
    "PID-13": _XTN, "PID-14": _XTN, "PID-18": _CX,
    "PV1-3": _PL, "PV1-7": _XCN, "PV1-8": _XCN, "PV1-17": _XCN, "PV1-19": _CX,
    "NK1-2": _XPN, "NK1-3": _CE, "NK1-4": _XAD, "NK1-5": _XTN,
    "AL1-3": _CE, "DG1-3": _CE, "MRG-1": _CX,
    "ORC-2": _EI, "ORC-3": _EI, "ORC-12": _XCN,
    "OBR-2": _EI, "OBR-3": _EI, "OBR-4": _CE, "OBR-16": _XCN, "OBR-31": _CE,
    "OBX-3": _CE, "OBX-6": _CE, "OBX-16": _XCN,
    "SPM-4": _CE, "ERR-3": _CE,
}  # fmt: skip

# Meanings of codes, in plain words. Unknown codes are shown as sent.
MEANINGS: dict[str, dict[str, str]] = {
    "MSH-11": {"P": "production", "T": "training", "D": "debugging"},
    "PID-8": {
        "F": "female",
        "M": "male",
        "O": "other",
        "U": "unknown",
        "A": "ambiguous",
        "N": "not applicable",
    },
    "PV1-2": {
        "E": "emergency",
        "I": "inpatient",
        "O": "outpatient",
        "P": "preadmit",
        "R": "recurring patient",
        "B": "obstetrics",
        "C": "commercial account",
        "N": "not applicable",
        "U": "unknown",
    },
    "ORC-1": {
        "NW": "new order",
        "CA": "cancel order",
        "RE": "observations to follow",
        "SC": "status changed",
        "XO": "change order",
        "OC": "order cancelled",
        "DC": "discontinue order",
        "HD": "hold order",
    },
    "OBR-25": {
        "O": "order received, no specimen yet",
        "I": "in progress, no results yet",
        "S": "scheduled",
        "A": "some results available",
        "P": "preliminary",
        "C": "correction to results",
        "R": "results stored, not yet verified",
        "F": "final",
        "X": "cannot obtain results",
    },
    "OBX-2": {
        "NM": "numeric",
        "ST": "string",
        "TX": "text",
        "FT": "formatted text",
        "CE": "coded entry",
        "CWE": "coded with exceptions",
        "SN": "structured numeric",
        "DT": "date",
        "DTM": "date and time",
        "TS": "time stamp",
        "ED": "encapsulated data",
        "RP": "reference pointer",
    },
    "OBX-8": {
        "N": "normal",
        "L": "low",
        "H": "high",
        "LL": "critically low",
        "HH": "critically high",
        "A": "abnormal",
        "AA": "critically abnormal",
        "<": "below the measurable range",
        ">": "above the measurable range",
        "POS": "positive",
        "NEG": "negative",
    },
    "OBX-11": {
        "F": "final",
        "P": "preliminary",
        "C": "correction",
        "A": "amended",
        "D": "delete this result",
        "W": "posted in error",
        "X": "cannot be obtained",
        "R": "entered, not verified",
        "I": "pending",
        "S": "partial",
        "U": "status changed to final",
    },
    "MSA-1": {
        "AA": "accepted",
        "AE": "application error",
        "AR": "application reject",
        "CA": "commit accept",
        "CE": "commit error",
        "CR": "commit reject",
    },
}


@dataclass(frozen=True, slots=True)
class Component:
    position: str  # "PID-5.1"
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class FieldLine:
    position: str  # "PID-5", or "PID-3[2]" for a second repetition
    name: str
    value: str
    meaning: str = ""
    components: tuple[Component, ...] = ()


@dataclass(frozen=True, slots=True)
class SegmentView:
    segment_id: str
    number: int  # 1 for the first OBX, 2 for the second
    name: str
    fields: tuple[FieldLine, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Separators:
    field: str
    component: str
    repetition: str
    escape: str
    subcomponent: str


def split_messages(text: str) -> list[str]:
    """A file can hold several messages: each starts at an MSH segment."""
    segments = [s for s in text.replace("\r\n", "\r").replace("\n", "\r").split("\r") if s]
    messages: list[list[str]] = []
    for segment in segments:
        if segment.startswith("MSH") or not messages:
            messages.append([])
        messages[-1].append(segment)
    return ["\r".join(m) for m in messages]


def _separators(msh: str) -> Separators:
    if not msh.startswith("MSH") or len(msh) < 8:
        raise HL7ParseError("no MSH segment with separators at the start")
    sep = msh[3]
    encoding = msh[4:].split(sep, 1)[0]
    if len(encoding) < 4 or sep in encoding or sep.isalnum():
        raise HL7ParseError("MSH-1 and MSH-2 do not hold usable separators")
    return Separators(sep, encoding[0], encoding[1], encoding[2], encoding[3])


def _unescape(value: str, seps: Separators) -> str:
    """Decode the standard escapes (\\F\\ \\S\\ \\T\\ \\R\\ \\E\\ and \\.br\\).
    Any other escape is left as sent."""
    e = seps.escape
    if e not in value:
        return value
    table = {
        "F": seps.field,
        "S": seps.component,
        "T": seps.subcomponent,
        "R": seps.repetition,
        "E": seps.escape,
        ".br": "\n",
    }
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == e:
            end = value.find(e, i + 1)
            if end != -1 and value[i + 1 : end] in table:
                out.append(table[value[i + 1 : end]])
                i = end + 1
                continue
        out.append(value[i])
        i += 1
    return "".join(out)


def _clean(value: str) -> str:
    return printable(value, VALUE_LIMIT)


def _field_lines(
    segment_id: str,
    pos: int,
    raw: str,
    seps: Separators,
    *,
    show_all: bool,
    value_type: str = "",
) -> Iterator[FieldLine]:
    ref = f"{segment_id}-{pos}"
    name = FIELDS.get(segment_id, {}).get(pos, "")
    if segment_id == "MSH" and pos in (1, 2):
        yield FieldLine(ref, name, _clean(raw))
        return
    repetitions = raw.split(seps.repetition) if raw else [""]
    for rep_no, rep in enumerate(repetitions, start=1):
        if not rep and not show_all:
            continue
        position = ref if rep_no == 1 else f"{ref}[{rep_no}]"
        parts = rep.split(seps.component)
        component_names = COMPONENTS.get(ref, ())
        if ref == "OBX-5" and value_type in ("CE", "CWE", "CNE"):
            component_names = _CE  # OBX-2 says what OBX-5 holds
        components: tuple[Component, ...] = ()
        valued = [p for p in parts if p]
        if len(parts) > 1 and (len(valued) > 1 or show_all):
            components = tuple(
                Component(
                    f"{ref}.{i}",
                    component_names[i - 1] if i <= len(component_names) else "",
                    _clean(_unescape(part, seps)),
                )
                for i, part in enumerate(parts, start=1)
                if part or show_all
            )
        meaning = ""
        codes = MEANINGS.get(ref)
        if codes is not None and rep:
            code = _unescape(parts[0], seps)
            meaning = codes.get(code, "(not a known code)")
        yield FieldLine(position, name, _clean(_unescape(rep, seps)), meaning, components)


def explain_message(text: str, *, show_all: bool = False) -> list[SegmentView]:
    """Every segment of one message, with its named fields."""
    segments = [s for s in text.replace("\r\n", "\r").replace("\n", "\r").split("\r") if s]
    if not segments:
        raise HL7ParseError("empty message")
    seps = _separators(segments[0])
    seen: dict[str, int] = {}
    views: list[SegmentView] = []
    for segment in segments:
        if segment.startswith("MSH"):
            # MSH-1 is the separator itself, so positions shift by one.
            raw_fields = [seps.field, *segment[4:].split(seps.field)]
            segment_id = "MSH"
        else:
            segment_id, _, rest = segment.partition(seps.field)
            raw_fields = rest.split(seps.field) if rest else []
        if len(segment_id) != 3 or not segment_id.isalnum():
            raise HL7ParseError("a segment id is not three letters or digits")
        seen[segment_id] = seen.get(segment_id, 0) + 1
        lines: list[FieldLine] = []
        value_type = raw_fields[1] if segment_id == "OBX" and len(raw_fields) > 1 else ""
        for pos, raw in enumerate(raw_fields, start=1):
            if not raw and not show_all:
                continue
            lines.extend(
                _field_lines(segment_id, pos, raw, seps, show_all=show_all, value_type=value_type)
            )
        if segment_id in SEGMENTS:
            name = SEGMENTS[segment_id]
        elif segment_id.startswith("Z"):
            name = "site-specific segment"
        else:
            name = ""
        views.append(SegmentView(segment_id, seen[segment_id], name, tuple(lines)))
    return views


def render_text(views: list[SegmentView]) -> str:
    out: list[str] = []
    width = max((len(f.position) for v in views for f in v.fields), default=6)
    for view in views:
        number = f" {view.number}" if view.number > 1 else ""
        out.append(f"{view.segment_id}{number}  {view.name}".rstrip())
        for f in view.fields:
            label = f"{f.position.ljust(width)} {f.name}".rstrip()
            value = f.value if f.value else "(empty)"
            meaning = f" = {f.meaning}" if f.meaning else ""
            out.append(f"  {label}: {value}{meaning}")
            for c in f.components:
                c_label = f"{c.position} {c.name}".rstrip()
                out.append(f"      {c_label}: {c.value if c.value else '(empty)'}")
    return "\n".join(out)


def as_json(views: list[SegmentView]) -> list[dict[str, object]]:
    return [
        {
            "segment": v.segment_id,
            "number": v.number,
            "name": v.name,
            "fields": [
                {
                    "position": f.position,
                    "name": f.name,
                    "value": f.value,
                    "meaning": f.meaning,
                    "components": [
                        {"position": c.position, "name": c.name, "value": c.value}
                        for c in f.components
                    ],
                }
                for f in v.fields
            ],
        }
        for v in views
    ]
