"""Helpers the segment mappers share: identifiers, codes and times."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import tzinfo

from fhir.resources.R4B.codeableconcept import CodeableConcept
from fhir.resources.R4B.coding import Coding
from fhir.resources.R4B.identifier import Identifier

from ..config import HospitalConfig
from ..errors import MappingError, TimestampError
from ..hl7time import BirthValue, is_date_only, parse_birth, parse_hl7_ts
from ..store.db import utc_iso
from .fields import FieldReader
from .tables import CODING_SYSTEMS, V2_0203

# A coding system name from the message becomes part of a URN, so only
# plain names are kept; anything else is refused, never cleaned up.
_SYSTEM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
OWN_CODING_SYSTEM = "urn:hl7bridge:coding-system:"
# A FHIR token search (``identifier=<system>|<value>``) treats these as
# syntax: ``,`` separates alternatives, ``|`` system and value, ``$``
# composite parts, and ``\`` escapes the others.
TOKEN_SPECIALS = (",", "|", "\\", "$")


@dataclass(frozen=True, slots=True)
class MapContext:
    hospital: HospitalConfig
    reader: FieldReader
    # The default zone for times with no offset: MSH-7's offset when it
    # has one, else the hospital's zone (``hl7time.message_tz``).
    tz: tzinfo


def check_key(value: str, field: str, *, segment: int = 1) -> str:
    """Refuse an identifier value that goes into a conditional URL
    (``<Type>?identifier=<system>|<value>``) when it holds a FHIR token
    special character or a control character. The check runs on the value
    after HL7 unescaping, so ``\\F\\`` (a ``|``) is caught too. A key is never
    escaped or cleaned up: a value that cannot be searched for exactly
    cannot keep a resend idempotent. Returns the value."""
    if any(ch in value for ch in TOKEN_SPECIALS) or any(
        unicodedata.category(ch) == "Cc" for ch in value
    ):
        raise MappingError(
            field, "holds a character that cannot be part of a FHIR search key", segment=segment
        )
    return value


def typed_identifier(type_code: str, system: str, value: str) -> Identifier:
    """An identifier with a v2 table 0203 type code (MR, ACSN, PLAC, VN)."""
    return Identifier(
        type=CodeableConcept(coding=[Coding(system=V2_0203, code=type_code)]),
        system=system,
        value=value,
    )


def procedure_system(hospital_id: str, code_system: str | None) -> str:
    """The procedure code system for a hospital. OBR-4 component 3 is not
    read: the profile's ``procedure_code_system`` names it, as on the store
    path, so both paths write the same ServiceRequest and report codes."""
    return f"urn:hl7bridge:{hospital_id.lower()}:procedure-codes:{code_system or 'local'}"


def procedure_concept(
    hospital_id: str, code_system: str | None, code: str, desc: str
) -> CodeableConcept | None:
    if not code and not desc:
        return None
    coding = None
    if code:
        coding = [
            Coding(
                system=procedure_system(hospital_id, code_system),
                code=code,
                display=desc or None,
            )
        ]
    return CodeableConcept(coding=coding, text=desc or code)


def coding_system(name: str, field: str, *, segment: int = 1) -> str | None:
    """The URI for a CE or CWE coding system name (component 3): a known
    name maps to its URI, another plain name is kept in hl7bridge's own
    namespace, and an empty name gives no system."""
    if not name:
        return None
    if name in CODING_SYSTEMS.codes:
        return CODING_SYSTEMS.codes[name]
    if not _SYSTEM_NAME.fullmatch(name):
        raise MappingError(field, "coding system name is not a plain name", segment=segment)
    return OWN_CODING_SYSTEM + name


def coded_concept(
    reader: FieldReader, ref: str, *, seg: int = 1, rep: int = 1
) -> CodeableConcept | None:
    """A CE or CWE field as a CodeableConcept: components 1 to 3, then the
    alternate code in 4 to 6, and CWE original text in component 9."""
    codings: list[Coding] = []
    for code_c, text_c, system_c in ((1, 2, 3), (4, 5, 6)):
        code = reader.get(ref, comp=code_c, seg=seg, rep=rep)
        if not code:
            continue
        display = reader.get(ref, comp=text_c, seg=seg, rep=rep)
        system = coding_system(reader.get(ref, comp=system_c, seg=seg, rep=rep), ref, segment=seg)
        codings.append(Coding(system=system, code=code, display=display or None))
    original = reader.get(ref, comp=9, seg=seg, rep=rep)
    text = original or reader.get(ref, comp=2, seg=seg, rep=rep) or None
    if not codings and not text:
        return None
    return CodeableConcept(coding=codings or None, text=text)


def timestamp(ctx: MapContext, ref: str, *, seg: int = 1, instant: bool = False) -> str | None:
    """An HL7 timestamp for a FHIR dateTime (or, with ``instant``, an
    instant), or None when empty.

    A value with a time becomes an ISO 8601 UTC string. A value with no
    offset is read in the message's zone: MSH-7's offset, else the
    hospital's (``MapContext.tz``). A date with no time stays a date
    (``2026-10-07``), as a FHIR dateTime allows; an instant needs a time,
    so there a date alone is refused rather than padded to midnight.
    """
    value = ctx.reader.get(ref, seg=seg)
    if not value:
        return None
    try:
        parsed = parse_hl7_ts(value, default_tz=ctx.tz)
        if is_date_only(value):
            if instant:
                raise MappingError(
                    ref, "has a date but no time; the FHIR element needs an instant", segment=seg
                )
            return parsed.date().isoformat()
        return utc_iso(parsed)
    except (TimestampError, OverflowError, ValueError):
        raise MappingError(ref, "not a valid HL7 timestamp", segment=seg) from None


def birth_date(ctx: MapContext, ref: str) -> BirthValue | None:
    """A date of birth as a FHIR date and, with minutes or finer, the
    birth time (``hl7time.parse_birth``), or None when empty."""
    value = ctx.reader.get(ref)
    if not value:
        return None
    try:
        return parse_birth(value, default_tz=ctx.tz)
    except (TimestampError, OverflowError, ValueError):
        raise MappingError(ref, "not a valid HL7 date") from None
