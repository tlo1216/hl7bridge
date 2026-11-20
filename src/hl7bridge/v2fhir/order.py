"""ORC and OBR to ServiceRequest (IG page ConceptMap-segment-orc-to-servicerequest).

The key is the accession, read from the field the hospital profile names
(``accession_field``), with type ACSN. The store path keys ServiceRequest
the same way, so ``to-fhir --post`` and ``fhir sync`` update one resource.
"""

from __future__ import annotations

from typing import Any

from fhir.resources.R4B.codeableconcept import CodeableConcept
from fhir.resources.R4B.identifier import Identifier
from fhir.resources.R4B.reference import Reference
from fhir.resources.R4B.servicerequest import ServiceRequest

from ..errors import MappingError
from .common import MapContext, check_key, procedure_concept, timestamp, typed_identifier
from .tables import ORDER_STATUS


def accession_value(ctx: MapContext) -> str:
    field = ctx.hospital.accession_field
    accession = ctx.reader.get(field)
    if not accession:
        raise MappingError(field, "no accession in the field the hospital profile names")
    return check_key(accession, field)


def accession_identifier(ctx: MapContext) -> Identifier:
    return typed_identifier("ACSN", ctx.hospital.systems.accession, accession_value(ctx))


def procedure_code(ctx: MapContext) -> CodeableConcept | None:
    h = ctx.hospital
    return procedure_concept(
        h.hospital_id,
        h.procedure_code_system,
        ctx.reader.get("OBR-4", comp=1),
        ctx.reader.get("OBR-4", comp=2),
    )


def orc_obr_to_service_request(ctx: MapContext, patient_ref: str) -> ServiceRequest:
    """One ORC and OBR pair. ``authoredOn`` is written for a new order
    (NW) only: a cancel (CA) is not when the order was written, and the
    post keeps the time the NW set (the ServiceRequest is merged)."""
    r = ctx.reader
    if r.count("ORC") != 1 or r.count("OBR") != 1:
        raise MappingError("ORC/OBR", "an ORM must hold exactly one order (ORC and OBR)")
    control = r.get("ORC-1")
    if not control:
        raise MappingError("ORC-1", "no order control code")
    status = ORDER_STATUS.lookup(control, "ORC-1")
    identifiers = [accession_identifier(ctx)]
    placer = r.get("ORC-2") or r.get("OBR-2")
    if placer:
        identifiers.append(typed_identifier("PLAC", ctx.hospital.systems.placer, placer))
    authored = None
    if control == "NW":
        authored = timestamp(ctx, "ORC-9") or timestamp(ctx, "MSH-7")
    kwargs: dict[str, Any] = {}
    code = procedure_code(ctx)
    if code is not None:
        kwargs["code"] = code
    if authored:
        kwargs["authoredOn"] = authored
    return ServiceRequest(
        identifier=identifiers,
        status=status,
        intent="order",
        subject=Reference(reference=patient_ref),
        **kwargs,
    )
