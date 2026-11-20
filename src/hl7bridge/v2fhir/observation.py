"""OBX (and the NTE after it) to Observation (IG page
ConceptMap-segment-obx-to-observation).

Two functions here decide the most:

- ``observation_identifier``: the key that makes a resend update the same
  Observation instead of adding one.
- ``obx_value``: OBX-2 picks the value[x] element, and a value that does
  not fit its type is refused, never stored as a string.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from fhir.resources.R4B.annotation import Annotation
from fhir.resources.R4B.identifier import Identifier
from fhir.resources.R4B.observation import Observation, ObservationReferenceRange
from fhir.resources.R4B.quantity import Quantity
from fhir.resources.R4B.reference import Reference

from ..errors import MappingError, TimestampError
from ..hl7time import parse_hl7_ts
from .common import MapContext, coded_concept, timestamp
from .tables import INTERPRETATION, OBSERVATION_STATUS, UCUM, V3_INTERPRETATION, VALUE_TYPE

# HL7 NM: an optional sign, digits, an optional decimal point. Nothing else:
# no comparator, no exponent, no thousands separator.
_NM = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
# A FHIR token search splits on these, so none may sit inside a key part.
_KEY_FORBIDDEN = ("/", "|", ",", "$", "\\")
TEXT_TYPES = ("ST", "TX", "FT")


def observation_identifier(
    system: str, filler: str, code: str, sub_id: str, *, set_id: str, segment: int
) -> Identifier:
    """The idempotency key of one result: ``<filler>/<OBX-3.1>/<OBX-4>``.

    The filler is the report's accession. OBX-4 (sub-id) tells apart
    results that share a code. When OBX-4 is empty the OBX-1 set id stands
    in (``set-<n>``), because a text report repeats one code on every line
    with no sub-id. A part that is empty, or holds a character that a FHIR
    token search treats as special, is refused: a key is never patched up.
    Two results with the same key in one message are refused by the bundle
    builder.
    """
    if not code:
        raise MappingError("OBX-3", "no observation code; it is part of the key", segment=segment)
    if sub_id:
        last = sub_id
        last_field = "OBX-4"
    elif set_id:
        last = f"set-{set_id}"
        last_field = "OBX-1"
    else:
        raise MappingError(
            "OBX-4", "no sub-id and no OBX-1 set id; the result has no key", segment=segment
        )
    for part, field in ((filler, "accession"), (code, "OBX-3"), (last, last_field)):
        if any(ch in part for ch in _KEY_FORBIDDEN):
            raise MappingError(
                field, "holds a character that cannot be part of a result key", segment=segment
            )
    return Identifier(system=system, value=f"{filler}/{code}/{last}")


def obx_value(ctx: MapContext, seg: int) -> dict[str, Any]:
    """The value[x] element for one OBX, chosen by OBX-2.

    - NM: valueQuantity. OBX-5 must be a plain number; ``>100`` or ``1e3``
      is refused (a comparator belongs in an SN value). OBX-6 becomes the
      unit, and the UCUM system and code are set only when OBX-6
      component 3 says UCUM.
    - ST, TX, FT: valueString; repetitions are joined with a line break.
    - CE, CWE: valueCodeableConcept.
    - DT: valueDateTime as a date; DTM and TS: valueDateTime in UTC.
    - Any other OBX-2, or an empty one under a value, raises. A value is
      never passed through as a string because its type is unknown.

    An empty OBX-5 gives no value element.
    """
    r = ctx.reader
    value_type = r.get("OBX-2", seg=seg)
    repeats = r.repetitions("OBX-5", seg=seg)
    if repeats == 0:
        return {}
    if not value_type:
        raise MappingError("OBX-2", "OBX-5 has a value but no value type", segment=seg)
    element = VALUE_TYPE.lookup(value_type, "OBX-2", segment=seg)
    if value_type in TEXT_TYPES:
        lines = [r.get("OBX-5", rep=i, seg=seg) for i in range(1, repeats + 1)]
        return {element: "\n".join(lines)}
    if repeats > 1:
        raise MappingError(
            "OBX-5", f"repeats, which is mapped only for text types, not {value_type}", segment=seg
        )
    if value_type == "NM":
        return {element: _quantity(ctx, seg)}
    if value_type in ("CE", "CWE"):
        return {element: coded_concept(r, "OBX-5", seg=seg)}
    # DT, DTM, TS
    if value_type == "DT":
        raw = r.get("OBX-5", seg=seg)
        try:
            day = parse_hl7_ts(raw, default_tz=ctx.hospital.tzinfo).date().isoformat()
        except TimestampError:
            raise MappingError("OBX-5", "not a valid HL7 date", segment=seg) from None
        return {element: day}
    return {element: timestamp(ctx, "OBX-5", seg=seg)}


def _quantity(ctx: MapContext, seg: int) -> Quantity:
    r = ctx.reader
    raw = r.get("OBX-5", seg=seg)
    if not _NM.fullmatch(raw):
        raise MappingError("OBX-5", "OBX-2 is NM but the value is not a plain number", segment=seg)
    unit_code = r.get("OBX-6", comp=1, seg=seg)
    unit_text = r.get("OBX-6", comp=2, seg=seg)
    unit_system = r.get("OBX-6", comp=3, seg=seg)
    kwargs: dict[str, Any] = {}
    if unit_system == "UCUM" and unit_code:
        kwargs = {"unit": unit_text or unit_code, "system": UCUM, "code": unit_code}
    elif unit_code or unit_text:
        # No code without a system: FHIR requires both or neither.
        kwargs = {"unit": unit_text or unit_code}
    return Quantity(value=Decimal(raw), **kwargs)


def obx_to_observation(
    ctx: MapContext, seg: int, *, filler: str, patient_ref: str, notes: list[str]
) -> Observation:
    r = ctx.reader
    status_code = r.get("OBX-11", seg=seg)
    if not status_code:
        raise MappingError(
            "OBX-11", "no result status; Observation.status is required", segment=seg
        )
    status = OBSERVATION_STATUS.lookup(status_code, "OBX-11", segment=seg)
    code = coded_concept(r, "OBX-3", seg=seg)
    if code is None:
        raise MappingError("OBX-3", "no observation code", segment=seg)
    identifier = observation_identifier(
        ctx.hospital.systems.observation,
        filler,
        r.get("OBX-3", comp=1, seg=seg),
        r.get("OBX-4", seg=seg),
        set_id=r.get("OBX-1", seg=seg),
        segment=seg,
    )
    kwargs: dict[str, Any] = obx_value(ctx, seg)

    ref_range = r.get("OBX-7", seg=seg)
    if ref_range:
        kwargs["referenceRange"] = [ObservationReferenceRange(text=ref_range)]

    flags = [r.get("OBX-8", rep=i, seg=seg) for i in range(1, r.repetitions("OBX-8", seg=seg) + 1)]
    flags = [f for f in flags if f]
    if flags:
        kwargs["interpretation"] = [
            {
                "coding": [
                    {
                        "system": V3_INTERPRETATION,
                        "code": INTERPRETATION.lookup(f, "OBX-8", segment=seg),
                    }
                ]
            }
            for f in flags
        ]

    effective = timestamp(ctx, "OBX-14", seg=seg)
    if effective:
        kwargs["effectiveDateTime"] = effective

    if notes:
        kwargs["note"] = [Annotation(text=text) for text in notes]

    return Observation(
        identifier=[identifier],
        status=status,
        code=code,
        subject=Reference(reference=patient_ref),
        **kwargs,
    )


def notes_after(ctx: MapContext) -> dict[int, list[str]]:
    """NTE-3 text by the OBX it follows (by OBX segment number). An NTE
    before the first OBX belongs to the order, not a result, and is not
    mapped here."""
    r = ctx.reader
    found: dict[int, list[str]] = {}
    obx_count = 0
    nte_count = 0
    current = 0  # the OBX an NTE here belongs to, 0 for none
    for name in r.order():
        if name == "OBX":
            obx_count += 1
            current = obx_count
        elif name == "NTE":
            nte_count += 1
            if current:
                reps = r.repetitions("NTE-3", seg=nte_count)
                text = "\n".join(r.get("NTE-3", rep=i, seg=nte_count) for i in range(1, reps + 1))
                if text:
                    found.setdefault(current, []).append(text)
        else:
            current = 0
    return found
