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
    v2: str  # a field position, "PID-5", or "(derived)" for an element no field fills
    fhir: str
    ig: str  # the IG page followed, or "" for none
    note: str = ""

    @property
    def is_field(self) -> bool:
        return not self.v2.startswith("(")


DERIVED = "(derived)"


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


_BASED_ON_NOTE = (
    "A logical reference to the ServiceRequest by its accession identifier"
    " (type ServiceRequest, identifier ACSN). The ORU does not write the order,"
    " and no server lookup is needed."
)


def _based_on_rows() -> list[SpecRow]:
    return [
        SpecRow("obr_to_diagnostic_report", ref, "DiagnosticReport.basedOn", "", _BASED_ON_NOTE)
        for ref in ALLOWED_ACCESSION_FIELDS
    ]


_TIME_NOTE = (
    " A time with no offset takes MSH-7's offset, else the hospital's zone; a date"
    " with no time stays a date."
)


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
    SpecRow(
        "message",
        "MSH-7",
        "(time zone)",
        "",
        "Its UTC offset, when it has one, is the zone of every time in the message that"
        " has none, as HL7 v2 defines it. Without one the hospital profile's zone is used.",
    ),
    # PID to Patient
    SpecRow(
        "pid_to_patient",
        "PID-3",
        "Patient.identifier",
        PID,
        "Every repetition (CX). The MRN repetition is written first, as before: component 1"
        " with the profile's mrn system and type MR; the first repetition when none has a type"
        " code (PID-3.5), else the first one typed MR; a typed list with no MR is refused. It"
        " is the key (conditional updates, Committed.key); only ADT writes the Patient, and ORM"
        " and ORU create it when missing. The other repetitions follow in message order, with"
        " the type from component 5 (v2-0203) and the assigning authority from component 4: an"
        " ISO OID, UUID or URI universal id becomes the system, anything else the namespace as"
        " assigner.display. Two other repetitions with the same value and no system differ by"
        " type or assigning authority and are both kept. Check digit and effective dates are not"
        " read. The IG takes every system from the authority; hl7bridge reads one MRN system"
        " per hospital.",
    ),
    SpecRow(
        "pid_to_patient",
        "PID-5",
        "Patient.name",
        PID,
        "Every repetition (XPN): component 1 family, 2 and 3 given, 4 and 6 suffix, 5 prefix,"
        " 7 use through table 0200 (a code the IG leaves unmapped gives no use). Validity range"
        " and effective dates are not read. A post replaces the server's names with these; a"
        " field left out keeps them, an explicit HL7 null (two double quotes) clears them"
        " (v2.8.2 section 2.10.5).",
    ),
    SpecRow(
        "pid_to_patient",
        "PID-7",
        "Patient.birthDate",
        PID,
        "The date at the precision sent (year, year and month, or day); an offset never"
        " moves it. With minutes or finer, the IG's patient-birthTime extension on birthDate"
        " (`_birthDate`) holds the full dateTime: the wall time as sent, in PID-7's offset,"
        " else the message's zone (MSH-7's offset, else the hospital's), not moved to UTC, so"
        " its date is birthDate. MSH-7's offset applies as HL7 makes it the message default,"
        " even when daylight time on the birth date differed. Seconds are zero-filled when"
        " absent (FHIR needs them), and a time of all zeros is written as midnight. An hour"
        " alone gives no extension (the IG's condition is any value longer than 8; FHIR has"
        " no hour precision and the minutes would be invented). With a time and no offset of its"
        " own, a PID-7 in a zone whose offset on the birth date is not whole minutes (local"
        " mean time) is refused; a date alone is kept. A post replaces the time"
        " with birthDate, so a date-only PID-7 clears a stored time; other extensions on"
        " birthDate stay.",
    ),
    SpecRow("pid_to_patient", "PID-8", "Patient.gender", PID, "Through table 0001."),
    SpecRow(
        "pid_to_patient",
        "PID-11",
        "Patient.address",
        PID,
        "Every repetition (XAD): components 1 and 2 (lines), 3 city, 4 state, 5 postal code,"
        " 6 country, 7 type as use through table 0190 (M and SH type, HV and the rest are not"
        " written), 9 county as district. Validity range and addressee are not read. A post"
        " replaces the server's addresses with these; a message with no PID-11 leaves them, an"
        " explicit HL7 null (two double quotes) clears them.",
    ),
    SpecRow(
        "pid_to_patient",
        "PID-13",
        "Patient.telecom",
        PID,
        "Every repetition (XTN). System from component 3 (table 0202: Internet and X.400 email,"
        " FX fax, BP pager, else phone). Value, as the IG says: component 4 for an email, else the"
        " unformatted number (12), else component 1 (only when 12 is absent), else a number"
        " built from country (5, as +), area (6) and local number (7) joined by spaces; a"
        " repetition with any of these is written. Use from component 2 through table 0201 (PRN,"
        " WPN, PRS); a code the IG leaves unmapped (EMR, NET) gives no use, and the field's"
        " default (home for PID-13) applies only when component 2 is empty. Extension and rank"
        " are not written. A post replaces the server's telecom list with PID-13 and PID-14"
        " together; a message with neither leaves it, an explicit HL7 null (two double quotes)"
        " in either, with no number mapped from either, clears it.",
    ),
    SpecRow(
        "pid_to_patient",
        "PID-14",
        "Patient.telecom",
        PID,
        "As PID-13, every repetition; the use defaults to work, as the IG says for PID-14.",
    ),
    # MRG to the merged-away Patient (ADT^A40)
    SpecRow(
        "mrg_to_patient",
        "MRG-1",
        "Patient.identifier (MR), Patient.active, Patient.link",
        "",
        "ADT^A40 only. Component 1 is the prior MRN, with the profile's MRN system"
        " (component 5, when valued, must be MR; component 4, when valued on both MRG-1 and"
        " PID-3, must match). One PID, one MRG and one repetition; the same MRN as PID-3, an"
        " empty or HL7-null MRN are refused. The Patient is set inactive and linked"
        " replaced-by to the survivor (PID). The IG (1.0.0) has no MRG-to-Patient map and no"
        " ADT_A40 message map; its only MRG map, segment-mrg-to-account"
        " (https://hl7.org/fhir/uv/v2mappings/ConceptMap-segment-mrg-to-account.html), maps"
        " MRG-3 to Account.identifier and leaves MRG-1 unmapped. A post merges this into the"
        " current Patient (existing links stay, a contradicting or reversed merge is refused), or"
        " writes a bare inactive one when the server has none; the offline Bundle makes it a"
        " conditional create. The survivor gets no link.",
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
    SpecRow("pv1_to_encounter", "PV1-44", "Encounter.period.start", PV1, _TIME_NOTE.strip()),
    SpecRow(
        "pv1_to_encounter",
        "PV1-45",
        "Encounter.period.end",
        PV1,
        "When valued, Encounter.status is finished, as in the IG. A post merges the"
        " Encounter into the current one, so an empty PV1-44 or PV1-45 changes nothing.",
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
        "Transaction time, for a new order (NW) only; a cancel leaves the time the order"
        " was written." + _TIME_NOTE,
    ),
    SpecRow(
        "orc_obr_to_service_request",
        "MSH-7",
        "ServiceRequest.authoredOn",
        "",
        "Used when ORC-9 is empty, as the store does (NW only).",
    ),
    # OBR to DiagnosticReport
    *_accession_rows("obr_to_diagnostic_report", "DiagnosticReport"),
    *_based_on_rows(),
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
        "OBR-8 is not read, so no effectivePeriod is written." + _TIME_NOTE,
    ),
    SpecRow(
        "obr_to_diagnostic_report",
        "OBR-22",
        "DiagnosticReport.issued",
        OBR_DR,
        "An instant: a date with no time is refused, never padded to midnight.",
    ),
    SpecRow(
        "obr_to_diagnostic_report",
        "OBR-25",
        "DiagnosticReport.status",
        OBR_DR,
        "Through table 0123. Required.",
    ),
    SpecRow(
        "obr_to_diagnostic_report",
        "OBX-2",
        "DiagnosticReport.conclusion",
        "",
        "TX and FT mark an OBX as narrative: report text, not a result.",
    ),
    SpecRow(
        "obr_to_diagnostic_report",
        "OBX-5",
        "DiagnosticReport.conclusion, DiagnosticReport.presentedForm",
        OBR_DR,
        "The narrative OBX lines, every repetition read whole, joined with line breaks;"
        " the same text as a text/plain presentedForm. No Observation per line.",
    ),
    SpecRow(
        "obr_to_diagnostic_report",
        DERIVED,
        "DiagnosticReport.result",
        OBR_DR,
        "One reference per structured OBX. A post of a corrected report sets each"
        " Observation the report no longer carries to entered-in-error.",
    ),
    # OBX to Observation
    SpecRow(
        "obx_to_observation",
        "OBX-1",
        "Observation.identifier",
        "",
        "Set id, the last part of the key <accession>/<OBX-3.1>/set/<OBX-1> when OBX-4 is empty.",
    ),
    SpecRow(
        "obx_to_observation",
        "OBX-2",
        "Observation.value[x]",
        OBX,
        "Chooses the value element through the value type table. Required when OBX-5 has a"
        " value; an unknown type is refused. TX and FT are narrative and not mapped here.",
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
        "Sub-id, the last part of the key <accession>/<OBX-3.1>/sub/<OBX-4>. Key parts"
        " may not hold / or a FHIR search special character.",
    ),
    SpecRow(
        "obx_to_observation",
        "OBX-5",
        "Observation.value[x]",
        OBX,
        "NM must be a plain number, its digits kept as sent (12.50 stays 12.50); ST joins"
        " repetitions, each read whole; other types may not repeat.",
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
    SpecRow(
        "obx_to_observation", "OBX-14", "Observation.effectiveDateTime", OBX, _TIME_NOTE.strip()
    ),
    SpecRow(
        "obx_to_observation",
        "NTE-3",
        "Observation.note.text",
        NTE,
        "Each NTE after a structured OBX, repetitions read whole and joined. An NTE before"
        " the first OBX, or after a narrative one, is not mapped.",
    ),
)

MAPPERS = (
    "message",
    "pid_to_patient",
    "mrg_to_patient",
    "pv1_to_encounter",
    "orc_obr_to_service_request",
    "obr_to_diagnostic_report",
    "obx_to_observation",
)


def fields_by_mapper() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {m: set() for m in MAPPERS}
    for row in ROWS:
        if row.is_field:
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
        "or A08; hl7bridge maps all three with the same PID and PV1 rows. It has",
        "no map for ADT^A40 and none from MRG to Patient (only MRG to Account);",
        "the merged-away Patient and its `replaced-by` link are hl7bridge's own",
        "reading of FHIR's Patient.link, not an IG map.",
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
        "ADT^A40": "Patient (survivor), Patient (merged away: inactive, linked replaced-by)",
        "ORM^O01": "Patient (created when missing), ServiceRequest",
        "ORU^R01": "Patient (created when missing), Observation per structured OBX,"
        " DiagnosticReport",
    }
    for msg in SUPPORTED:
        lines.append(f"| {_cell(msg)} | {contents[msg]} |")
    lines += [
        "",
        "Each resource is keyed on its first identifier, inside one transaction",
        "Bundle. Observation and DiagnosticReport are conditional updates. Patient",
        "from ADT, Encounter and ServiceRequest are conditional updates that a post",
        "first merges into the current resource, because an empty v2 field means",
        "no change: an element the message has replaces the current one whole, one it",
        "lacks is kept. Patient from ORM and ORU is a conditional create: only ADT",
        "writes the Patient. Anything not listed is refused.",
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
