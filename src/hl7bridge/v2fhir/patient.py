"""PID to Patient (IG page ConceptMap-segment-pid-to-patient).

Only ADT messages write the Patient. An ORM or ORU carries a PID too, often
a thin one, so their Bundles create the Patient only when none has the MRN
yet and otherwise just refer to it (``bundle.py``).
"""

from __future__ import annotations

from typing import Any

from fhir.resources.R4B.address import Address
from fhir.resources.R4B.contactpoint import ContactPoint
from fhir.resources.R4B.humanname import HumanName
from fhir.resources.R4B.patient import Patient, PatientLink
from fhir.resources.R4B.reference import Reference

from ..errors import MappingError
from ..ingest.parser import pick_mrn_repetition
from . import hooks
from .common import MapContext, birth_date, check_key, typed_identifier
from .tables import SEX


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
    systems = ctx.hospital.systems
    kwargs: dict[str, Any] = {}

    family = r.get("PID-5", comp=1)
    given: list[str | None] = [g for g in (r.get("PID-5", comp=2), r.get("PID-5", comp=3)) if g]
    if family or given:
        kwargs["name"] = [HumanName(family=family or None, given=given or None)]

    sex = r.get("PID-8")
    if sex:
        kwargs["gender"] = hooks.sex_code(sex, lambda raw: SEX.lookup(raw, "PID-8"))

    born = birth_date(ctx, "PID-7")
    if born:
        kwargs["birthDate"] = born

    address = _address(ctx)
    if address is not None:
        kwargs["address"] = [address]

    telecom = _telecom(ctx)
    if telecom is not None:
        kwargs["telecom"] = [telecom]

    return Patient(identifier=[typed_identifier("MR", systems.mrn, mrn_value(ctx))], **kwargs)


def _address(ctx: MapContext) -> Address | None:
    """PID-11, first repetition (XAD): street, other line, city, state,
    postal code, country."""
    r = ctx.reader
    parts = {c: r.get("PID-11", comp=c) for c in range(1, 7)}
    if not any(parts.values()):
        return None
    lines: list[str | None] = [parts[c] for c in (1, 2) if parts[c]]
    return Address(
        line=lines or None,
        city=parts[3] or None,
        state=parts[4] or None,
        postalCode=parts[5] or None,
        country=parts[6] or None,
    )


def _telecom(ctx: MapContext) -> ContactPoint | None:
    """PID-13, first repetition (XTN). Component 4 (an email address) when
    component 3 says Internet, else component 1 as a phone number. The IG
    sets use to home for PID-13."""
    r = ctx.reader
    equipment = r.get("PID-13", comp=3)
    if equipment == "Internet":
        email = r.get("PID-13", comp=4)
        return ContactPoint(system="email", value=email, use="home") if email else None
    number = r.get("PID-13", comp=1)
    return ContactPoint(system="phone", value=number, use="home") if number else None


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
