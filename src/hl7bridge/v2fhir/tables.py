"""Code tables for the v2-to-FHIR mapping. Never guess.

Each table maps the HL7 v2 codes hl7bridge accepts to the FHIR code it
writes. A code that is not in the table raises ``MappingError`` with the
field position; it is never passed through, defaulted or echoed. Where
the HL7 v2-to-FHIR implementation guide (v1.0.0, STU 1) has a vocabulary
map, the table follows it and names its page in ``ig_map`` (under
https://hl7.org/fhir/uv/v2mappings/); codes that map leaves
unmapped are left out here too. The store path (``fhir/mapper.py``) uses
the same tables.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import MappingError

V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
V2_0004 = "http://terminology.hl7.org/CodeSystem/v2-0004"
V3_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
V3_INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
UCUM = "http://unitsofmeasure.org"
LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"


@dataclass(frozen=True, slots=True)
class CodeTable:
    """``codes`` maps a v2 code to the FHIR value written for it."""

    table: str  # "0001"
    name: str
    target: str  # the FHIR element or code system it feeds
    ig_map: str  # the IG page it follows, or "" when it follows none
    codes: dict[str, str]

    def lookup(self, code: str, field: str, *, segment: int = 1) -> str:
        try:
            return self.codes[code]
        except KeyError:
            raise MappingError(
                field,
                f"code not in table {self.table} ({self.name}); refusing to guess",
                segment=segment,
            ) from None


SEX = CodeTable(
    "0001",
    "administrative sex",
    "Patient.gender",
    "ConceptMap-table-hl70001-to-administrative-gender",
    {"F": "female", "M": "male", "O": "other", "U": "unknown", "A": "other", "N": "other"},
)

# Encounter.class is a Coding. The IG map sends E, I, O and P to v3 ActCode
# and keeps R, B, C, N and U as v2 table 0004 codes; the value here is
# "system|code".
PATIENT_CLASS = CodeTable(
    "0004",
    "patient class",
    "Encounter.class",
    "ConceptMap-table-hl70004-to-v3-actcode",
    {
        "E": f"{V3_ACT_CODE}|EMER",
        "I": f"{V3_ACT_CODE}|IMP",
        "O": f"{V3_ACT_CODE}|AMB",
        "P": f"{V3_ACT_CODE}|PRENC",
        "R": f"{V2_0004}|R",
        "B": f"{V2_0004}|B",
        "C": f"{V2_0004}|C",
        "N": f"{V2_0004}|N",
        "U": f"{V2_0004}|U",
    },
)

ENCOUNTER_STATUS = CodeTable(
    "0004",
    "patient class",
    "Encounter.status (when PV1-45 is empty)",
    "ConceptMap-table-hl70004-to-encounter-status",
    {
        "E": "in-progress",
        "I": "in-progress",
        "O": "in-progress",
        "P": "planned",
        "R": "in-progress",
        "B": "in-progress",
        "C": "in-progress",
        "N": "in-progress",
        "U": "unknown",
    },
)

OBSERVATION_STATUS = CodeTable(
    "0085",
    "observation result status",
    "Observation.status",
    "ConceptMap-table-hl70085-to-observation-status",
    {
        "A": "amended",
        "C": "corrected",
        "D": "entered-in-error",
        "F": "final",
        "P": "preliminary",
        "W": "entered-in-error",
        "X": "cancelled",
    },
)

REPORT_STATUS = CodeTable(
    "0123",
    "result status",
    "DiagnosticReport.status",
    "ConceptMap-table-hl70123-queries-to-diagnostic-report-status",
    {
        "O": "registered",
        "I": "registered",
        "S": "registered",
        "P": "preliminary",
        "C": "corrected",
        "R": "partial",
        "F": "final",
        "X": "cancelled",
    },
)

# Abnormal flags: the IG map copies each code into v3
# ObservationInterpretation unchanged, and leaves AC, HM, OBX, QCF and TOX
# unmapped. The value is the code written.
INTERPRETATION = CodeTable(
    "0078",
    "interpretation codes",
    "Observation.interpretation",
    "ConceptMap-table-hl70078-to-v3-observationinterpretation",
    {
        code: code
        for code in (
            "<", ">", "A", "AA", "B", "CAR", "D", "DET", "E", "EX", "EXP", "H", "HH", "HU",
            "I", "IE", "IND", "L", "LL", "LU", "MS", "N", "NCL", "ND", "NEG", "NR", "NS",
            "POS", "R", "RR", "S", "SDD", "SYN-R", "SYN-S", "U", "UNE", "VS", "W", "WR",
        )
    },
)  # fmt: skip

# ORC-1 to ServiceRequest.status. hl7bridge accepts only the two order
# controls the ingest pipeline handles, with the meanings the store path
# has always written. Not taken from an IG vocabulary map.
ORDER_STATUS = CodeTable(
    "0119",
    "order control",
    "ServiceRequest.status",
    "",
    {"NW": "active", "CA": "revoked"},
)

# OBX-2 value types hl7bridge maps, to the value[x] element written, as in
# the IG's OBX-5 rows. Anything else (SN, ED, RP, NR and the rest) raises.
VALUE_TYPE = CodeTable(
    "0125",
    "value type",
    "Observation.value[x]",
    "ConceptMap-segment-obx-to-observation (OBX-5 rows)",
    {
        "NM": "valueQuantity",
        "ST": "valueString",
        "TX": "valueString",
        "FT": "valueString",
        "CE": "valueCodeableConcept",
        "CWE": "valueCodeableConcept",
        "DT": "valueDateTime",
        "DTM": "valueDateTime",
        "TS": "valueDateTime",
    },
)

# Coding system names in CE and CWE component 3 that have a known URI.
# Any other name is kept in hl7bridge's own namespace (see coding_system).
CODING_SYSTEMS = CodeTable(
    "0396",
    "coding system",
    "Coding.system",
    "",
    {"LN": LOINC, "SCT": SNOMED, "UCUM": UCUM},
)

TABLES: tuple[CodeTable, ...] = (
    SEX,
    PATIENT_CLASS,
    ENCOUNTER_STATUS,
    OBSERVATION_STATUS,
    REPORT_STATUS,
    INTERPRETATION,
    ORDER_STATUS,
    VALUE_TYPE,
    CODING_SYSTEMS,
)
