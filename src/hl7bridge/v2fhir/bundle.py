"""One HL7 v2 message to one FHIR transaction Bundle. No sockets, no store.

Entries refer to each other by ``urn:uuid`` full URLs, which the server
resolves inside the transaction. The uuids are derived from the
identifiers, so the same message always gives the same Bundle (the
golden-file tests depend on it). Each entry has a write policy:

- ``replace``: ``PUT <Type>?identifier=<system>|<value>``, a conditional
  update. Observation and DiagnosticReport: the ORU carries all of them.
- ``merge``: the same PUT, which a post first merges into the server's
  current resource. Patient from ADT, Encounter, ServiceRequest.
- ``create``: ``POST <Type>`` with ``ifNoneExist``, a conditional create.
  Patient from ORM and ORU: created when missing, never changed.

Only ADT writes the Patient. In HL7 v2 an empty field means "no change",
so a ``merge`` entry never erases what the message leaves out: the post
reads the current resource and writes it back with what the message
carries (``hl7bridge.fhir.post``). The Bundle printed offline shows the
entry before that merge.

Supported messages, by MSH-9:

| Message | Entries |
| --- | --- |
| ADT^A01, ADT^A04, ADT^A08 | Patient, and Encounter when PV1-19 holds a visit number |
| ORM^O01 | Patient, ServiceRequest |
| ORU^R01 | Patient, one Observation per structured OBX, DiagnosticReport |

Anything else raises ``MappingError``: ADT^A40 (merges wait for the A40
work), ACKs, and every type the hospital profile does not list.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from fhir.resources.R4B.bundle import Bundle, BundleEntry, BundleEntryRequest
from fhir.resources.R4B.resource import Resource
from pydantic import ValidationError

from ..config import HospitalConfig, HospitalRegistry
from ..errors import HL7ParseError, MappingError
from ..hl7time import message_tz
from ..ingest.parser import parse_hl7
from .common import MapContext, check_key
from .encounter import pv1_to_encounter
from .fields import FieldReader
from .jsontext import resource_dict
from .observation import KEY_JOIN, notes_after, obx_to_observation
from .order import accession_value, orc_obr_to_service_request
from .patient import pid_to_patient
from .report import is_narrative, obr_to_diagnostic_report, report_text

ADT_EVENTS = ("ADT^A01", "ADT^A04", "ADT^A08")
SUPPORTED = (*ADT_EVENTS, "ORM^O01", "ORU^R01")
REPLACE, MERGE, CREATE = "replace", "merge", "create"
# A fixed namespace for the uuid5 full URLs, so they are stable across runs.
_NAMESPACE = uuid.UUID("6f1c2b0e-3a52-5a7e-9d0c-6c1f6a0b8e41")


@dataclass(frozen=True, slots=True)
class MappedMessage:
    bundle: Bundle
    hospital_id: str
    message_type: str
    touched: dict[str, set[str]]
    # The write policy of each entry, by its full URL (see the module text).
    policies: dict[str, str] = field(default_factory=dict)
    # For an ORU: the report's accession and the Observation identifier
    # system, which a post needs to find the results a correction dropped.
    accession: str = ""
    observation_system: str = ""

    def entry_types(self) -> list[str]:
        return [e.resource.get_resource_type() for e in self.bundle.entry or [] if e.resource]


def full_url(resource_type: str, system: str, value: str) -> str:
    """The stable ``urn:uuid`` of a resource with this first identifier."""
    return f"urn:uuid:{uuid.uuid5(_NAMESPACE, f'{resource_type}|{system}|{value}')}"


def _full_url(resource: Resource) -> str:
    ident = resource.identifier[0]  # type: ignore[attr-defined]
    return full_url(resource.get_resource_type(), ident.system, ident.value)


def search_query(system: str, value: str) -> str:
    """``identifier=<system>|<value>`` for a conditional URL, percent-encoded.

    The value has passed ``check_key`` (no ``,`` ``|`` ``\\`` ``$`` or
    control character), so the token needs no FHIR escaping and the one
    ``|`` is the system separator. It is checked again here so that no
    path can build a conditional URL around an unchecked value.
    """
    check_key(value, "identifier")
    return "identifier=" + quote(f"{system}|{value}", safe=":/|")


def _entry(resource: Resource, policy: str) -> BundleEntry:
    ident = resource.identifier[0]  # type: ignore[attr-defined]
    kind = resource.get_resource_type()
    query = search_query(ident.system, ident.value)
    if policy == CREATE:
        request = BundleEntryRequest(method="POST", url=kind, ifNoneExist=query)
    else:
        request = BundleEntryRequest(method="PUT", url=f"{kind}?{query}")
    return BundleEntry(fullUrl=_full_url(resource), resource=resource, request=request)


@contextmanager
def _mapping(reader: FieldReader, name: str, where: str, *, segment: int = 1) -> Iterator[None]:
    """Attribute reads to mapper ``name``, and turn a model validation error
    into a MappingError on segment ``where``. A pydantic error quotes the
    value it refused, so only its type survives: never its text."""
    with reader.mapper(name):
        try:
            yield
        except ValidationError:
            raise MappingError(
                where, "a value is not valid for its FHIR element", segment=segment
            ) from None


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
    reason, never a value) for anything it cannot map, input that is not
    HL7 included."""
    try:
        message = parse_hl7(text)
    except HL7ParseError:
        raise MappingError("MSH", "not parseable as HL7 v2") from None
    reader = FieldReader(message)
    with reader.mapper("message"):
        hospital, msg_type = _route(reader, registry)
        tz = message_tz(reader.get("MSH-7"), hospital.tzinfo)
    ctx = MapContext(hospital=hospital, reader=reader, tz=tz)

    with _mapping(reader, "pid_to_patient", "PID"):
        patient = pid_to_patient(ctx)
    patient_ref = _full_url(patient)
    is_adt = msg_type in ADT_EVENTS
    planned: list[tuple[Resource, str]] = [(patient, MERGE if is_adt else CREATE)]

    if is_adt:
        with _mapping(reader, "pv1_to_encounter", "PV1"):
            encounter = pv1_to_encounter(ctx, patient_ref)
        if encounter is not None:
            planned.append((encounter, MERGE))
    elif msg_type == "ORM^O01":
        with _mapping(reader, "orc_obr_to_service_request", "ORC"):
            planned.append((orc_obr_to_service_request(ctx, patient_ref), MERGE))
    else:
        planned.extend((r, REPLACE) for r in _report_resources(ctx, patient_ref))

    seen: set[str] = set()
    for resource, _ in planned:
        url = _full_url(resource)
        if url in seen:
            raise MappingError(
                "OBX-3/OBX-4", "two results in one message share a key (code and sub-id)"
            )
        seen.add(url)
    bundle = Bundle(type="transaction", entry=[_entry(r, p) for r, p in planned])
    report = planned[-1][0] if msg_type == "ORU^R01" else None
    return MappedMessage(
        bundle=bundle,
        hospital_id=hospital.hospital_id,
        message_type=msg_type,
        touched={name: set(refs) for name, refs in reader.touched.items()},
        policies={_full_url(r): p for r, p in planned},
        accession=report.identifier[0].value if report is not None else "",  # type: ignore[attr-defined]
        observation_system=hospital.systems.observation if report is not None else "",
    )


def _report_resources(ctx: MapContext, patient_ref: str) -> list[Resource]:
    reader = ctx.reader
    if reader.count("OBR") != 1:
        raise MappingError("OBR", "an ORU must hold exactly one OBR; more are not mapped")
    with _mapping(reader, "obr_to_diagnostic_report", "OBR"):
        filler = accession_value(ctx)
        if KEY_JOIN in filler:
            # The accession is the first part of every result key, and a
            # post finds a report's own results by it. Refused for every
            # ORU, narrative ones included, so no report can claim another
            # report's results.
            raise MappingError(
                ctx.hospital.accession_field,
                "holds a character that cannot be part of a result key",
            )
        text = report_text(ctx)
    observations: list[Resource] = []
    with reader.mapper("obx_to_observation"):
        notes = notes_after(ctx)
    for seg in range(1, reader.count("OBX") + 1):
        with reader.mapper("obr_to_diagnostic_report"):
            narrative = is_narrative(ctx, seg)
        if narrative:
            continue
        with _mapping(reader, "obx_to_observation", "OBX", segment=seg):
            observations.append(
                obx_to_observation(
                    ctx, seg, filler=filler, patient_ref=patient_ref, notes=notes.get(seg, [])
                )
            )
    with _mapping(reader, "obr_to_diagnostic_report", "OBR"):
        report = obr_to_diagnostic_report(
            ctx,
            patient_ref,
            result_refs=[_full_url(o) for o in observations],
            report_text=text,
        )
    return [*observations, report]


def bundle_json(mapped: MappedMessage) -> dict[str, Any]:
    """The Bundle as a JSON-ready dict, empty elements left out, decimals
    kept as ``Decimal`` (write it with ``jsontext.dumps``)."""
    return resource_dict(mapped.bundle)


__all__ = [
    "ADT_EVENTS",
    "CREATE",
    "MERGE",
    "REPLACE",
    "SUPPORTED",
    "MappedMessage",
    "bundle_json",
    "full_url",
    "message_to_bundle",
    "search_query",
]
