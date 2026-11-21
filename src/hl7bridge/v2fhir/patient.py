"""PID to Patient (IG page ConceptMap-segment-pid-to-patient).

Only ADT messages write the Patient. An ORM or ORU carries a PID too, often
a thin one, so their Bundles create the Patient only when none has the MRN
yet and otherwise just refer to it (``bundle.py``).
"""

from __future__ import annotations

import re
from typing import Any

from fhir.resources.R4B.address import Address
from fhir.resources.R4B.codeableconcept import CodeableConcept
from fhir.resources.R4B.coding import Coding
from fhir.resources.R4B.contactpoint import ContactPoint
from fhir.resources.R4B.humanname import HumanName
from fhir.resources.R4B.identifier import Identifier
from fhir.resources.R4B.patient import Patient, PatientLink
from fhir.resources.R4B.reference import Reference

from ..errors import MappingError
from ..ingest.parser import pick_mrn_repetition
from . import hooks
from .common import MapContext, birth_date, check_key, typed_identifier
from .tables import SEX, V2_0203


def mrn_value(ctx: MapContext) -> str:
    """PID-3 component 1 of the MRN repetition: the key of the Patient.

    With no identifier type (PID-3.5) in any repetition, the first one is
    the MRN. When the repetitions are typed, the first one typed MR is,
    and a typed list with no MR is refused (``pick_mrn_repetition``).
    """
    r = ctx.reader
    reps = r.repetitions("PID-3")
    types = [r.get("PID-3", rep=n, comp=5) for n in range(1, reps + 1)]
    chosen = pick_mrn_repetition(types)
    if chosen is None and reps:
        raise MappingError("PID-3", "has typed identifiers (PID-3.5) and none is MR")
    mrn = r.get("PID-3", rep=chosen) if chosen else ""
    if not mrn:
        raise MappingError("PID-3", "no patient identifier; a Patient needs one as its key")
    return check_key(mrn, "PID-3")


def mrn_authority(ctx: MapContext) -> str:
    """PID-3.4 (assigning authority, its first subcomponent) of the MRN
    repetition, or ''. Direct mode picks the MRN system by it
    (``fhir.identifier_systems.mrn_by_authority``)."""
    r = ctx.reader
    reps = r.repetitions("PID-3")
    chosen = pick_mrn_repetition([r.get("PID-3", rep=n, comp=5) for n in range(1, reps + 1)])
    if not chosen:
        return ""
    return r.get("PID-3", rep=chosen, comp=4).split("&", 1)[0]


def pid_to_patient(ctx: MapContext) -> Patient:
    r = ctx.reader
    if not r.count("PID"):
        raise MappingError("PID-3", "the message has no PID segment")
    kwargs: dict[str, Any] = {}

    names = _names(ctx)
    if names:
        kwargs["name"] = names

    sex = r.get("PID-8")
    if sex:
        kwargs["gender"] = hooks.sex_code(sex, lambda raw: SEX.lookup(raw, "PID-8"))

    born = birth_date(ctx, "PID-7")
    if born:
        kwargs["birthDate"] = born

    addresses = _addresses(ctx)
    if addresses:
        kwargs["address"] = addresses

    telecom = _telecoms(ctx, "PID-13", "home") + _telecoms(ctx, "PID-14", "work")
    if telecom:
        kwargs["telecom"] = _distinct(telecom)

    return Patient(identifier=_identifiers(ctx), **kwargs)


def _distinct(items: list[Any]) -> list[Any]:
    """The items without exact repeats, in order."""
    seen: set[str] = set()
    out = []
    for item in items:
        key = item.model_dump_json()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _value(ctx: MapContext, ref: str, rep: int, comp: int) -> str:
    """One component, with the HL7 null (two double quotes) read as empty."""
    value = ctx.reader.get(ref, rep=rep, comp=comp)
    return "" if value == '""' else value


_OID = re.compile(r"[0-2](?:\.(?:0|[1-9][0-9]*))+")
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_TYPE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,19}")
_URI = re.compile(r"(?:https?|urn):[^\s]{1,200}")


def _authority(ctx: MapContext, rep: int) -> tuple[str | None, str | None]:
    """PID-3.4 (HD: namespace, universal id, universal id type) of one
    repetition as (system, assigner display). A universal id of type ISO
    (an OID), UUID or URI becomes the system URI; otherwise the namespace
    (or the bare universal id) names the assigner, as the IG's CX map says
    for an authority that is an organization."""
    name, uid, kind = (
        p if p != '""' else ""
        for p in (ctx.reader.get("PID-3", rep=rep, comp=4, sub=n) for n in (1, 2, 3))
    )
    if uid:
        if kind == "ISO" and _OID.fullmatch(uid):
            return f"urn:oid:{uid}", None
        if kind == "UUID" and _UUID.fullmatch(uid):
            return f"urn:uuid:{uid.lower()}", None
        if kind == "URI" and _URI.fullmatch(uid):
            return uid, None
    return None, (name or uid or None)


def _identifiers(ctx: MapContext) -> list[Identifier]:
    """Every PID-3 repetition (CX), the MRN one first. The MRN repetition
    is the key (conditional updates, ``Committed.key``) and is written
    exactly as before: the profile's mrn system, type MR. The others take
    their type from PID-3.5 and their system or assigner from PID-3.4."""
    r = ctx.reader
    reps = r.repetitions("PID-3")
    chosen = pick_mrn_repetition([r.get("PID-3", rep=n, comp=5) for n in range(1, reps + 1)])
    out = [typed_identifier("MR", ctx.hospital.systems.mrn, mrn_value(ctx))]
    seen: set[tuple[str | None, str | None, str, str | None]] = {
        (out[0].system, out[0].value, "MR", None)
    }
    for n in range(1, reps + 1):
        if n == chosen:
            continue
        value = _value(ctx, "PID-3", n, 1)
        if not value:
            continue
        code = _value(ctx, "PID-3", n, 5)
        system, assigner = _authority(ctx, n)
        # With no system, the assigning authority tells two numbers apart.
        fingerprint = (system, value, code, None if system else assigner)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(
            Identifier(
                type=(
                    CodeableConcept(coding=[Coding(system=V2_0203, code=code)])
                    if _TYPE_CODE.fullmatch(code)
                    else None
                ),
                system=system,
                value=value,
                assigner=Reference(display=assigner) if assigner else None,
            )
        )
    return out


# IG ConceptMap-table-hl70200-to-name-use; codes it leaves unmapped give no use.
NAME_USE = {
    "BAD": "old",
    "D": "usual",
    "L": "official",
    "M": "maiden",
    "MSK": "anonymous",
    "N": "nickname",
    "NAV": "temp",
    "R": "official",
    "TEMP": "temp",
}
# IG ConceptMap-table-hl70190-to-address-use.
ADDRESS_USE = {
    "BA": "old",
    "BI": "billing",
    "C": "temp",
    "B": "work",
    "H": "home",
    "O": "work",
}
# IG ConceptMap-table-hl70201-to-contact-point-use.
TELECOM_USE = {"PRN": "home", "WPN": "work", "PRS": "mobile"}
# Table 0202 equipment types to ContactPoint.system.
TELECOM_SYSTEM = {"FX": "fax", "BP": "pager", "Internet": "email", "X.400": "email"}


def _names(ctx: MapContext) -> list[HumanName]:
    """PID-5, every repetition (XPN): family (1), given (2, 3), suffix (4,
    degree 6), prefix (5) and use (7, through table 0200)."""
    out = []
    for n in range(1, ctx.reader.repetitions("PID-5") + 1):
        c = {k: _value(ctx, "PID-5", n, k) for k in range(1, 8)}
        given: list[str | None] = [g for g in (c[2], c[3]) if g]
        suffix: list[str | None] = [x for x in (c[4], c[6]) if x]
        prefix: list[str | None] = [c[5]] if c[5] else []
        if not (c[1] or given or suffix or prefix):
            continue
        out.append(
            HumanName(
                family=c[1] or None,
                given=given or None,
                suffix=suffix or None,
                prefix=prefix or None,
                use=NAME_USE.get(c[7]),
            )
        )
    return _distinct(out)


def _addresses(ctx: MapContext) -> list[Address]:
    """PID-11, every repetition (XAD): street (1), other line (2), city,
    state, postal code, country (3 to 6), type (7: a use through table
    0190) and county (9, as district)."""
    out = []
    for n in range(1, ctx.reader.repetitions("PID-11") + 1):
        p = {k: _value(ctx, "PID-11", n, k) for k in (1, 2, 3, 4, 5, 6, 7, 9)}
        if not any(p.values()):
            continue
        lines: list[str | None] = [p[k] for k in (1, 2) if p[k]]
        out.append(
            Address(
                use=ADDRESS_USE.get(p[7]),
                line=lines or None,
                city=p[3] or None,
                state=p[4] or None,
                district=p[9] or None,
                postalCode=p[5] or None,
                country=p[6] or None,
            )
        )
    return _distinct(out)


def _xtn_value(ctx: MapContext, ref: str, rep: int, system: str) -> str:
    """The ContactPoint value of one XTN repetition, by the IG's rules: the
    email address (XTN.4) for an email, else the unformatted number (XTN.12),
    else XTN.1 (only when XTN.12 is absent), else a number built from the
    country code (XTN.5), area code (XTN.6) and local number (XTN.7)."""
    if system == "email":
        email = _value(ctx, ref, rep, 4)
        if email:
            return email
    unformatted = _value(ctx, ref, rep, 12)
    if unformatted:
        return unformatted
    legacy = _value(ctx, ref, rep, 1)
    if legacy:
        return legacy
    country, area, local = (_value(ctx, ref, rep, k) for k in (5, 6, 7))
    if not local and not area:
        return ""  # a country code alone is not a number
    parts = ([f"+{country}"] if country else []) + [p for p in (area, local) if p]
    return " ".join(parts)


def _telecoms(ctx: MapContext, ref: str, default_use: str) -> list[ContactPoint]:
    """One telecom field, every repetition (XTN). The value follows the IG
    (``_xtn_value``); a repetition with a value is never dropped. The use is
    component 2 through table 0201; a code the IG leaves unmapped (EMR, NET)
    gives no use, and the field's default (home for PID-13, work for PID-14)
    applies only when component 2 is empty. The system is component 3 through
    table 0202, phone when that is empty."""
    out = []
    for n in range(1, ctx.reader.repetitions(ref) + 1):
        system = TELECOM_SYSTEM.get(_value(ctx, ref, n, 3), "phone")
        value = _xtn_value(ctx, ref, n, system)
        if not value:
            continue
        code = _value(ctx, ref, n, 2)
        use = TELECOM_USE.get(code) if code else default_use
        out.append(ContactPoint(system=system, value=value, use=use))
    return out


def _is_null(ctx: MapContext, ref: str) -> bool:
    """True when a repetition of the field is the HL7 null (two double
    quotes in its first component)."""
    return any(
        ctx.reader.get(ref, rep=n, comp=1) == '""'
        for n in range(1, ctx.reader.repetitions(ref) + 1)
    )


def cleared_elements(ctx: MapContext, patient: Patient) -> frozenset[str]:
    """The Patient elements the message explicitly nulls (HL7 v2.8.2
    section 2.10.5: the null in a repeating field deletes it). A field left
    out keeps the server's value; an explicit null with nothing else mapped
    from it clears the element. Telecom is one list built from PID-13 and
    PID-14, so a null in either with no number mapped from either clears it."""
    out = set()
    for element, refs in (
        ("name", ("PID-5",)),
        ("address", ("PID-11",)),
        ("telecom", ("PID-13", "PID-14")),
    ):
        if not getattr(patient, element, None) and any(_is_null(ctx, ref) for ref in refs):
            out.add(element)
    return frozenset(out)


def merged_patient(
    ctx: MapContext, survivor_ref: str, survivor_mrn: str, survivor_authority: str = ""
) -> Patient:
    """MRG-1 to the Patient that an ADT^A40 merged away: inactive, with a
    ``replaced-by`` link to the surviving Patient. Only the MRN is known,
    so the post merges this into the current Patient (nothing else of it
    changes, existing links stay) and, when the server has none, writes a
    bare inactive stub rather than guess its demographics. The survivor
    gets no link: a second merge into it would otherwise have to rewrite
    the first. One PID and one MRG only: a message with repeated groups
    would silently lose all but the first, so it is refused."""
    r = ctx.reader
    if not r.count("MRG"):
        raise MappingError("MRG-1", "the message has no MRG segment")
    if r.count("MRG") > 1 or r.count("PID") > 1:
        raise MappingError("MRG-1", "more than one PID/MRG group; not mapped")
    reps = r.repetitions("MRG-1")
    if reps > 1:
        raise MappingError("MRG-1", "more than one merged identifier; not mapped")
    mrn = r.get("MRG-1")
    if not mrn or mrn == '""':
        # '""' is HL7's explicit null; it is not an identifier.
        raise MappingError("MRG-1", "no prior patient identifier; a merge needs one")
    id_type = r.get("MRG-1", comp=5)
    if id_type and id_type != "MR":
        raise MappingError("MRG-1", "identifier type (MRG-1.5) is not MR")
    authority = r.get("MRG-1", comp=4).split("&", 1)[0]
    if authority and survivor_authority and authority != survivor_authority:
        raise MappingError("MRG-1", "assigning authority (MRG-1.4) differs from PID-3.4")
    if check_key(mrn, "MRG-1") == survivor_mrn:
        raise MappingError("MRG-1", "is the same patient identifier as PID-3")
    return Patient(
        identifier=[typed_identifier("MR", ctx.hospital.systems.mrn, mrn)],
        active=False,
        link=[PatientLink(other=Reference(reference=survivor_ref), type="replaced-by")],
    )
