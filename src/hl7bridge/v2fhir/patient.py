"""PID to Patient (IG page ConceptMap-segment-pid-to-patient)."""

from __future__ import annotations

from typing import Any

from fhir.resources.R4B.address import Address
from fhir.resources.R4B.contactpoint import ContactPoint
from fhir.resources.R4B.humanname import HumanName
from fhir.resources.R4B.patient import Patient

from ..errors import MappingError
from .common import MapContext, birth_date, typed_identifier
from .tables import SEX


def mrn_value(ctx: MapContext) -> str:
    """PID-3, first repetition, component 1: the key of the Patient."""
    mrn = ctx.reader.get("PID-3")
    if not mrn:
        raise MappingError("PID-3", "no patient identifier; a Patient needs one as its key")
    return mrn


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
        kwargs["gender"] = SEX.lookup(sex, "PID-8")

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
