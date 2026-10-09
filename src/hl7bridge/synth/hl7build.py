"""Minimal HL7 v2 message builder for the synthetic generator.

Builder inputs are generator-controlled constants, so instead of escaping we
assert that no field value smuggles in a separator; a violation is a bug in
the generator and must fail loudly, not produce a subtly corrupt message.
"""

from __future__ import annotations

from datetime import datetime

from ..hl7time import format_hl7_ts

_FORBIDDEN = ("|", "\r", "\n", "\x0b", "\x1c")


def seg(*fields: str) -> str:
    for f in fields:
        for ch in _FORBIDDEN:
            if ch in f:
                raise ValueError(f"field value {f!r} contains separator {ch!r}")
    return "|".join(fields)


def message(*segments: str) -> str:
    return "\r".join(segments) + "\r"


def msh(
    *,
    sending_app: str,
    sending_facility: str,
    receiving_app: str,
    receiving_facility: str,
    ts: datetime,
    include_offset: bool,
    message_type: str,
    control_id: str,
    version: str,
) -> str:
    # MSH-1/MSH-2 are the separators themselves; build by hand.
    return seg(
        "MSH",
        "^~\\&",
        sending_app,
        sending_facility,
        receiving_app,
        receiving_facility,
        format_hl7_ts(ts, include_offset=include_offset),
        "",
        message_type,
        control_id,
        "P",
        version,
    )


def obr(
    *,
    set_id: str = "1",
    placer: str = "",
    filler: str = "",
    procedure_code: str = "",
    procedure_desc: str = "",
    code_system: str = "",
    observed: str = "",
    result_ts: str = "",
    modality: str = "",
    status: str = "",
) -> str:
    fields = [""] * 26
    fields[0] = "OBR"
    fields[1] = set_id
    fields[2] = placer
    fields[3] = filler
    fields[4] = f"{procedure_code}^{procedure_desc}^{code_system}".rstrip("^")
    fields[7] = observed
    fields[22] = result_ts
    fields[24] = modality
    fields[25] = status
    return seg(*fields)
