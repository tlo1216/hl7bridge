"""One HL7 v2 message to one FHIR transaction Bundle. No sockets, no store.

Each entry is a conditional update, ``PUT <Type>?identifier=<system>|<value>``,
so posting the same message twice updates in place. Entries refer to each
other by ``urn:uuid`` full URLs, which the server resolves inside the
transaction. The uuids are derived from the identifiers, so the same
message always gives the same Bundle (the golden-file tests depend on it).

Supported messages, by MSH-9:

| Message | Entries |
| --- | --- |
| ADT^A01, ADT^A04, ADT^A08 | Patient, and Encounter when PV1-19 holds a visit number |
| ORM^O01 | Patient, ServiceRequest |
| ORU^R01 | Patient, one Observation per OBX, DiagnosticReport |

Anything else raises ``MappingError``: ADT^A40 (merges wait for the A40
work), ACKs, and every type the hospital profile does not list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from fhir.resources.R4B.bundle import Bundle, BundleEntry, BundleEntryRequest
from fhir.resources.R4B.resource import Resource

from ..config import HospitalConfig, HospitalRegistry
from ..errors import HL7ParseError, MappingError
from ..ingest.parser import parse_hl7
from .common import MapContext
from .encounter import pv1_to_encounter
from .fields import FieldReader
from .observation import notes_after, obx_to_observation
from .order import accession_value, orc_obr_to_service_request
from .patient import pid_to_patient
from .report import obr_to_diagnostic_report

ADT_EVENTS = ("ADT^A01", "ADT^A04", "ADT^A08")
SUPPORTED = (*ADT_EVENTS, "ORM^O01", "ORU^R01")
# A fixed namespace for the uuid5 full URLs, so they are stable across runs.
_NAMESPACE = uuid.UUID("6f1c2b0e-3a52-5a7e-9d0c-6c1f6a0b8e41")


@dataclass(frozen=True, slots=True)
class MappedMessage:
    bundle: Bundle
    hospital_id: str
    message_type: str
    touched: dict[str, set[str]]

    def entry_types(self) -> list[str]:
        return [e.resource.get_resource_type() for e in self.bundle.entry or [] if e.resource]


def _full_url(resource: Resource) -> str:
    ident = resource.identifier[0]  # type: ignore[attr-defined]
    name = f"{resource.get_resource_type()}|{ident.system}|{ident.value}"
    return f"urn:uuid:{uuid.uuid5(_NAMESPACE, name)}"


def _entry(resource: Resource) -> BundleEntry:
    ident = resource.identifier[0]  # type: ignore[attr-defined]
    query = quote(f"{ident.system}|{ident.value}", safe=":/|")
    return BundleEntry(
        fullUrl=_full_url(resource),
        resource=resource,
        request=BundleEntryRequest(
            method="PUT", url=f"{resource.get_resource_type()}?identifier={query}"
        ),
    )


def _route(reader: FieldReader, registry: HospitalRegistry) -> tuple[HospitalConfig, str]:
    """The hospital (MSH-4, then MSH-6) and the message type (MSH-9)."""
    hospital = registry.match_facility(reader.get("MSH-4"), reader.get("MSH-6"))
    if hospital is None:
        raise MappingError("MSH-4/MSH-6", "match no configured hospital")
    kind, trigger = reader.get("MSH-9", comp=1), reader.get("MSH-9", comp=2)
    msg_type = f"{kind}^{trigger}" if trigger else kind
    if msg_type not in SUPPORTED:
        raise MappingError("MSH-9", "message type is not mapped to FHIR by hl7bridge")
    if msg_type not in hospital.expected_message_types:
        raise MappingError("MSH-9", "message type is not listed in the hospital profile")
    return hospital, msg_type


def message_to_bundle(text: str, registry: HospitalRegistry) -> MappedMessage:
    """Map one message. Raises MappingError (field position and a fixed
    reason, never a value) or HL7ParseError for input that is not HL7."""
    try:
        message = parse_hl7(text)
    except HL7ParseError:
        raise MappingError("MSH", "not parseable as HL7 v2") from None
    reader = FieldReader(message)
    with reader.mapper("message"):
        hospital, msg_type = _route(reader, registry)
    ctx = MapContext(hospital=hospital, reader=reader)

    with reader.mapper("pid_to_patient"):
        patient = pid_to_patient(ctx)
    patient_ref = _full_url(patient)
    resources: list[Resource] = [patient]

    if msg_type in ADT_EVENTS:
        with reader.mapper("pv1_to_encounter"):
            encounter = pv1_to_encounter(ctx, patient_ref)
        if encounter is not None:
            resources.append(encounter)
    elif msg_type == "ORM^O01":
        with reader.mapper("orc_obr_to_service_request"):
            resources.append(orc_obr_to_service_request(ctx, patient_ref))
    else:
        resources.extend(_report_resources(ctx, patient_ref))

    seen: set[str] = set()
    for resource in resources:
        url = _full_url(resource)
        if url in seen:
            raise MappingError(
                "OBX-3/OBX-4", "two results in one message share a key (code and sub-id)"
            )
        seen.add(url)
    bundle = Bundle(type="transaction", entry=[_entry(r) for r in resources])
    return MappedMessage(
        bundle=bundle,
        hospital_id=hospital.hospital_id,
        message_type=msg_type,
        touched={name: set(refs) for name, refs in reader.touched.items()},
    )


def _report_resources(ctx: MapContext, patient_ref: str) -> list[Resource]:
    reader = ctx.reader
    if reader.count("OBR") != 1:
        raise MappingError("OBR", "an ORU must hold exactly one OBR; more are not mapped")
    with reader.mapper("obr_to_diagnostic_report"):
        filler = accession_value(ctx)
    observations: list[Resource] = []
    with reader.mapper("obx_to_observation"):
        notes = notes_after(ctx)
        for seg in range(1, reader.count("OBX") + 1):
            observations.append(
                obx_to_observation(
                    ctx, seg, filler=filler, patient_ref=patient_ref, notes=notes.get(seg, [])
                )
            )
        report_text = _report_text(ctx)
    with reader.mapper("obr_to_diagnostic_report"):
        report = obr_to_diagnostic_report(
            ctx,
            patient_ref,
            result_refs=[_full_url(o) for o in observations],
            report_text=report_text,
        )
    return [*observations, report]


def _report_text(ctx: MapContext) -> str:
    """The text lines of the report (OBX with ST, TX or FT), one per line,
    as the store path joins them for ``conclusion``."""
    reader = ctx.reader
    lines: list[str] = []
    for seg in range(1, reader.count("OBX") + 1):
        if reader.get("OBX-2", seg=seg) in ("ST", "TX", "FT"):
            for rep in range(1, reader.repetitions("OBX-5", seg=seg) + 1):
                line = reader.get("OBX-5", rep=rep, seg=seg)
                if line:
                    lines.append(line)
    return "\n".join(lines)


def bundle_json(mapped: MappedMessage) -> dict[str, Any]:
    """The Bundle as a JSON-ready dict, empty elements left out."""
    result: dict[str, Any] = mapped.bundle.model_dump(mode="json", exclude_none=True)
    return result


__all__ = ["SUPPORTED", "MappedMessage", "bundle_json", "message_to_bundle"]
