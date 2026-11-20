"""The mapping spec: the one source for what each mapper reads and writes.

``docs/mapping.md`` is generated from this module (``render_markdown``)
and a test fails when the file and the module differ. Another test runs
every mapper over the sample messages and fails when a mapper reads a
field that has no row here, or when a row names a field no mapper reads.

IG references name a page of the HL7 v2-to-FHIR implementation guide,
v1.0.0 STU 1 (https://hl7.org/fhir/uv/v2mappings/). The guide is cited
by page; its tables are not copied.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ALLOWED_ACCESSION_FIELDS
from .bundle import SUPPORTED
from .tables import TABLES

IG_BASE = "https://hl7.org/fhir/uv/v2mappings/"
IG_VERSION = "1.0.0 (STU 1)"


@dataclass(frozen=True, slots=True)
class SpecRow:
    mapper: str
    v2: str  # a field position, "PID-5"
    fhir: str
    ig: str  # the IG page followed, or "" for none
    note: str = ""


PID = "ConceptMap-segment-pid-to-patient"
PV1 = "ConceptMap-segment-pv1-to-encounter"
ORC = "ConceptMap-segment-orc-to-servicerequest"
OBR_DR = "ConceptMap-segment-obr-to-diagnosticreport"
OBX = "ConceptMap-segment-obx-to-observation"
NTE = "ConceptMap-segment-nte-to-observation"

_ACCESSION_NOTE = (
    "Whichever of {fields} the profile names in accession_field. Written as"
    " an identifier with type ACSN and the profile's accession system, and used"
    " as the key. The IG writes OBR-3 and ORC-3 as a FILL identifier;"
    " hl7bridge keys imaging on the accession, wherever the site puts it."
).format(fields=", ".join(ALLOWED_ACCESSION_FIELDS))


def _accession_rows(mapper: str, resource: str) -> list[SpecRow]:
    return [
        SpecRow(mapper, ref, f"{resource}.identifier (ACSN)", "", _ACCESSION_NOTE)
        for ref in ALLOWED_ACCESSION_FIELDS
    ]


ROWS: tuple[SpecRow, ...] = (
    # Routing, before any segment is mapped.
    SpecRow(
        "message", "MSH-4", "(routing)", "", "Sending facility, matched to a hospital profile."
    ),
    SpecRow(
        "message", "MSH-6", "(routing)", "", "Receiving facility, tried when MSH-4 matches none."
    ),
    SpecRow(
        "message",
        "MSH-9",
        "(routing)",
        "",
        "Message type and trigger. Must be one hl7bridge maps and one the profile lists.",
    ),
    # PID to Patient
    SpecRow(
        "pid_to_patient",
        "PID-3",
        "Patient.identifier (MR)",
        PID,
        "First repetition, component 1, with the profile's mrn system. The IG maps"
        " every repetition and takes the system from the assigning authority;"
        " hl7bridge reads one system per hospital.",
    ),
    SpecRow(
        "pid_to_patient",
        "PID-5",
        "Patient.name",
        PID,
        "First repetition: component 1 family, components 2 and 3 given.",
    ),
    SpecRow(
        "pid_to_patient",
        "PID-7",
        "Patient.birthDate",
        PID,
        "Date part only. The IG adds a birthTime extension for a longer value; not written.",
    ),
    SpecRow("pid_to_patient", "PID-8", "Patient.gender", PID, "Through table 0001."),
    SpecRow(
        "pid_to_patient",
        "PID-11",
        "Patient.address",
        PID,
        "First repetition, components 1 to 6 (lines, city, state, postal code, country).",
    ),
    SpecRow(
        "pid_to_patient",
        "PID-13",
        "Patient.telecom",
        PID,
        "First repetition. Component 4 as email when component 3 is Internet, else"
        " component 1 as phone. Use is home, as the IG says for PID-13.",
    ),
    # PV1 to Encounter
    SpecRow(
        "pv1_to_encounter",
        "PV1-2",
        "Encounter.class, Encounter.status",
        PV1,
        "Class through table 0004 to v3 ActCode (R, B, C, N, U stay v2-0004 codes)."
        " Status through the table 0004 status map when PV1-45 is empty.",
    ),
    SpecRow(
        "pv1_to_encounter",
        "PV1-19",
        "Encounter.identifier (VN)",
        PV1,
        "Component 1 with the profile's visit system; the key. No visit number, no Encounter.",
    ),
    SpecRow("pv1_to_encounter", "PV1-44", "Encounter.period.start", PV1),
    SpecRow(
        "pv1_to_encounter",
        "PV1-45",
        "Encounter.period.end",
        PV1,
        "When valued, Encounter.status is finished, as in the IG.",
    ),
    # ORC and OBR to ServiceRequest
    SpecRow(
        "orc_obr_to_service_request",
        "ORC-1",
        "ServiceRequest.status",
        ORC,
        "NW active, CA revoked; any other order control is refused. hl7bridge's own"
        " table, not the IG's table 0119 map.",
    ),
    SpecRow(
        "orc_obr_to_service_request",
        "ORC-2",
        "ServiceRequest.identifier (PLAC)",
        ORC,
        "Placer order number, with the profile's placer system. The IG prefers"
        " OBR-2 and falls back to ORC-2; hl7bridge reads ORC-2 first, as the store does.",
    ),
    SpecRow(
        "orc_obr_to_service_request",
        "OBR-2",
        "ServiceRequest.identifier (PLAC)",
        ORC,
        "Used when ORC-2 is empty.",
    ),
    *_accession_rows("orc_obr_to_service_request", "ServiceRequest"),
    SpecRow(
        "orc_obr_to_service_request",
        "OBR-4",
        "ServiceRequest.code",
        "",
        "Components 1 and 2. The coding system comes from the profile's"
        " procedure_code_system, not component 3, so both paths write one code.",
    ),
    SpecRow(
        "orc_obr_to_service_request",
        "ORC-9",
        "ServiceRequest.authoredOn",
        ORC,
        "Transaction time, in UTC.",
    ),
    SpecRow(
        "orc_obr_to_service_request",
        "MSH-7",
        "ServiceRequest.authoredOn",
        "",
        "Used when ORC-9 is empty, as the store does.",
    ),
    # OBR to DiagnosticReport
    *_accession_rows("obr_to_diagnostic_report", "DiagnosticReport"),
    SpecRow(
        "obr_to_diagnostic_report",
        "OBR-4",
        "DiagnosticReport.code",
        OBR_DR,
        "As for ServiceRequest.code. No code gives the text Imaging report.",
    ),
    SpecRow(
        "obr_to_diagnostic_report",
        "OBR-7",
        "DiagnosticReport.effectiveDateTime",
        OBR_DR,
        "OBR-8 is not read, so no effectivePeriod is written.",
    ),
    SpecRow("obr_to_diagnostic_report", "OBR-22", "DiagnosticReport.issued", OBR_DR),
    SpecRow(
        "obr_to_diagnostic_report",
        "OBR-25",
        "DiagnosticReport.status",
        OBR_DR,
        "Through table 0123. Required.",
    ),
    # OBX to Observation
    SpecRow(
        "obx_to_observation",
        "OBX-1",
        "Observation.identifier",
        "",
        "Set id, the last part of the key when OBX-4 is empty (set-<n>).",
    ),
    SpecRow(
        "obx_to_observation",
        "OBX-2",
        "Observation.value[x]",
        OBX,
        "Chooses the value element through the value type table. Required when OBX-5 has a"
        " value; an unknown type is refused. ST, TX and FT lines also form the report text.",
    ),
    SpecRow(
        "obx_to_observation",
        "OBX-3",
        "Observation.code",
        OBX,
        "CE or CWE. Component 1 is part of the key.",
    ),
    SpecRow(
        "obx_to_observation",
        "OBX-4",
        "Observation.identifier",
        "",
        "Sub-id, the last part of the key <accession>/<OBX-3.1>/<OBX-4>.",
    ),
    SpecRow(
        "obx_to_observation",
        "OBX-5",
        "Observation.value[x]",
        OBX,
        "NM must be a plain number; text types join repetitions; other types may not repeat.",
    ),
    SpecRow(
        "obx_to_observation",
        "OBX-6",
        "Observation.valueQuantity unit",
        OBX,
        "UCUM system and code only when component 3 is UCUM; otherwise the unit text alone.",
    ),
    SpecRow("obx_to_observation", "OBX-7", "Observation.referenceRange.text", OBX),
    SpecRow(
        "obx_to_observation",
        "OBX-8",
        "Observation.interpretation",
        OBX,
        "Every repetition, through table 0078.",
    ),
    SpecRow(
        "obx_to_observation",
        "OBX-11",
        "Observation.status",
        OBX,
        "Through table 0085. Required; codes the IG map leaves unmapped are refused.",
    ),
    SpecRow("obx_to_observation", "OBX-14", "Observation.effectiveDateTime", OBX),
    SpecRow(
        "obx_to_observation",
        "NTE-3",
        "Observation.note.text",
        NTE,
        "Each NTE after an OBX, repetitions joined. An NTE before the first OBX is not mapped.",
    ),
)

MAPPERS = (
    "message",
    "pid_to_patient",
    "pv1_to_encounter",
    "orc_obr_to_service_request",
    "obr_to_diagnostic_report",
    "obx_to_observation",
)


def fields_by_mapper() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {m: set() for m in MAPPERS}
    for row in ROWS:
        found[row.mapper].add(row.v2)
    return found


def _cell(text: str) -> str:
    return text.replace("|", "\\|") if text else ""


def _ig_cell(page: str) -> str:
    return f"[{page}]({IG_BASE}{page}.html)" if page else "none"


def render_markdown() -> str:
    """The text of ``docs/mapping.md``."""
    lines = [
        "# v2 to FHIR mapping",
        "",
        "Generated from `src/hl7bridge/v2fhir/spec.py` by",
        "`python tools/gen_mapping_doc.py`. Do not edit by hand: a test fails",
        "when this file and the spec differ, and another fails when a mapper",
        "reads a field that has no row here.",
        "",
        f"The IG column names a page of the HL7 v2-to-FHIR implementation guide,"
        f" {IG_VERSION}, at {IG_BASE}. The guide is cited by page; its tables are",
        "not copied. The guide has a message map for ADT^A01 and none for A04",
        "or A08; hl7bridge maps all three with the same PID and PV1 rows.",
        "",
        "Resources are FHIR R4B models (`fhir.resources`); servers are R4.",
        "Identifier systems come from the hospital profile (`fhir.identifier_systems`).",
        "",
        "## Messages",
        "",
        "| Message | Resources |",
        "| --- | --- |",
    ]
    contents = {
        "ADT^A01": "Patient, Encounter (when PV1-19 is valued)",
        "ADT^A04": "Patient, Encounter (when PV1-19 is valued)",
        "ADT^A08": "Patient, Encounter (when PV1-19 is valued)",
        "ORM^O01": "Patient, ServiceRequest",
        "ORU^R01": "Patient, Observation per OBX, DiagnosticReport",
    }
    for msg in SUPPORTED:
        lines.append(f"| {_cell(msg)} | {contents[msg]} |")
    lines += [
        "",
        "Each resource is a conditional update keyed on its first identifier,",
        "inside one transaction Bundle. Anything not listed is refused.",
    ]
    for mapper in MAPPERS:
        lines += [
            "",
            f"## `{mapper}`",
            "",
            "| v2 | FHIR | IG map | Notes |",
            "| --- | --- | --- | --- |",
        ]
        for row in ROWS:
            if row.mapper == mapper:
                lines.append(
                    f"| {row.v2} | {_cell(row.fhir)} | {_ig_cell(row.ig)} | {_cell(row.note)} |"
                )
    lines += [
        "",
        "## Code tables",
        "",
        "A code that is not listed is refused with the field position. Codes",
        "only; the code meanings are in the HL7 tables themselves.",
    ]
    for table in TABLES:
        pairs = ", ".join(
            f"`{_cell(code)}` {_cell(value.split('|')[-1])}" for code, value in table.codes.items()
        )
        lines += [
            "",
            f"### Table {table.table}, {table.name}, to {_cell(table.target)}",
            "",
            f"IG map: {_ig_cell(table.ig_map.split(' ')[0]) if table.ig_map else 'none'}",
            "",
            pairs,
        ]
    return "\n".join(lines) + "\n"
