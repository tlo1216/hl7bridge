"""PV1 to Encounter (IG page ConceptMap-segment-pv1-to-encounter).

One mapping for ADT A01, A04 and A08, keyed on PV1-19, so an A08 updates
the same Encounter. In HL7 v2 an empty field means "no change", not
"remove", so a post merges the Encounter into the server's current one
(``fhir/post.py``): an A08 with no PV1-44 keeps the admit time. A message
with no PV1, or a PV1 with no visit number, has no Encounter: without
PV1-19 there is no key to update it by.
"""

from __future__ import annotations

from typing import Any

from fhir.resources.R4B.coding import Coding
from fhir.resources.R4B.encounter import Encounter
from fhir.resources.R4B.period import Period
from fhir.resources.R4B.reference import Reference

from ..errors import MappingError
from .common import MapContext, check_key, timestamp, typed_identifier
from .tables import ENCOUNTER_STATUS, PATIENT_CLASS


def visit_number(ctx: MapContext) -> str:
    """PV1-19 component 1, or '' when the message has no visit number."""
    if not ctx.reader.count("PV1"):
        return ""
    visit = ctx.reader.get("PV1-19")
    return check_key(visit, "PV1-19") if visit else ""


def pv1_to_encounter(ctx: MapContext, patient_ref: str) -> Encounter | None:
    r = ctx.reader
    visit = visit_number(ctx)
    if not visit:
        return None
    patient_class = r.get("PV1-2")
    if not patient_class:
        raise MappingError("PV1-2", "no patient class; Encounter.class is required")
    system, code = PATIENT_CLASS.lookup(patient_class, "PV1-2").split("|", 1)
    start = timestamp(ctx, "PV1-44")
    end = timestamp(ctx, "PV1-45")
    # The IG sets status to finished when PV1-45 is valued, else maps PV1-2.
    status = "finished" if end else ENCOUNTER_STATUS.lookup(patient_class, "PV1-2")
    # "class" is a Python keyword; the model takes it by its FHIR name.
    kwargs: dict[str, Any] = {"class": Coding(system=system, code=code)}
    if start or end:
        kwargs["period"] = Period.model_validate({"start": start, "end": end})
    return Encounter(
        identifier=[typed_identifier("VN", ctx.hospital.systems.visit, visit)],
        status=status,
        subject=Reference(reference=patient_ref),
        **kwargs,
    )
