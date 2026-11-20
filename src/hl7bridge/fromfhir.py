"""FHIR to HL7 v2: sample messages for a patient that lives on a FHIR server.

``hl7bridge from-fhir --patient ID`` reads one Patient and writes four
messages with that patient's MRN in PID-3: an ADT^A04, an ADT^A08, an
ORM^O01 and an ORU^R01. A teaching lab needs them because the server's
patients (Synthea, say) have no v2 messages and their ids differ on every
deployment. ``--report ID`` instead renders a basic ORU^R01 from a
DiagnosticReport and its Observations.

The MRN is the identifier typed MR (v2 table 0203), else the first
identifier; ``pick_mrn`` says which, and the command prints it. Every
value from the server is HL7-escaped, so a ``|`` or ``^`` in a name cannot
shift a field. MSH-11 is T (training): the direct listener refuses P.
Codes in the sample order and result are local (``99HUB``), not LOINC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import HospitalConfig
from .errors import FhirPostError, UsageError
from .fhir.client import HapiClient
from .hl7time import format_hl7_ts
from .v2fhir.tables import V2_0203

PROCESSING_ID = "T"
_ESCAPES = (("\\", "\\E\\"), ("|", "\\F\\"), ("^", "\\S\\"), ("&", "\\T\\"), ("~", "\\R\\"))
_GENDER = {"male": "M", "female": "F", "other": "O", "unknown": "U"}
_REPORT_STATUS = {
    "registered": "I",
    "partial": "P",
    "preliminary": "P",
    "final": "F",
    "amended": "C",
    "corrected": "C",
    "appended": "C",
    "cancelled": "X",
    "entered-in-error": "X",
}
_OBS_STATUS = {
    "registered": "I",
    "preliminary": "P",
    "final": "F",
    "amended": "C",
    "corrected": "C",
    "cancelled": "X",
    "entered-in-error": "W",
}


def escape(value: object) -> str:
    """One value as HL7 v2 text: separators escaped, line breaks removed."""
    text = "" if value is None else str(value)
    for raw, escaped in _ESCAPES:
        text = text.replace(raw, escaped)
    return " ".join(text.replace("\r", "\n").split("\n")).strip()


@dataclass(frozen=True, slots=True)
class Mrn:
    value: str
    system: str
    typed_mr: bool


def pick_mrn(patient: dict[str, Any]) -> Mrn:
    """The identifier typed MR, else the first one with a value."""
    identifiers = [
        i for i in patient.get("identifier") or [] if isinstance(i, dict) and i.get("value")
    ]
    if not identifiers:
        raise UsageError("the Patient has no identifier with a value; nothing to put in PID-3.")
    for ident in identifiers:
        codings = (ident.get("type") or {}).get("coding") or []
        if any(c.get("system") == V2_0203 and c.get("code") == "MR" for c in codings):
            return Mrn(str(ident["value"]), str(ident.get("system", "")), True)
    first = identifiers[0]
    return Mrn(str(first["value"]), str(first.get("system", "")), False)


class Renderer:
    """Builds messages for one hospital profile at one time."""

    def __init__(self, hospital: HospitalConfig, now: datetime) -> None:
        self.hospital = hospital
        self.now = now.astimezone(hospital.tzinfo)
        self.stamp = format_hl7_ts(self.now, include_offset=hospital.timestamps_include_offset)
        self._seq = 0

    def _msh(self, message_type: str) -> str:
        self._seq += 1
        control = f"FF{self.now:%Y%m%d%H%M%S}{self._seq:02d}"
        h = self.hospital
        return "|".join(
            [
                "MSH",
                "^~\\&",
                "FROMFHIR",
                h.facility_codes[0],
                "HL7BRIDGE",
                "HUB",
                self.stamp,
                "",
                message_type,
                control,
                PROCESSING_ID,
                h.hl7_version,
            ]
        )

    def _authority(self, mrn: Mrn) -> str:
        for authority, system in self.hospital.fhir.mrn_by_authority:
            if system == mrn.system:
                return authority
        return self.hospital.hospital_id

    def pid(self, patient: dict[str, Any], mrn: Mrn) -> str:
        name = (patient.get("name") or [{}])[0]
        given = name.get("given") or []
        xpn = "^".join(
            [
                escape(name.get("family", "")),
                escape(given[0] if given else ""),
                escape(given[1] if len(given) > 1 else ""),
            ]
        ).rstrip("^")
        born = str(patient.get("birthDate", "")).replace("-", "")[:8]
        sex = _GENDER.get(str(patient.get("gender", "")), "")
        cx = f"{escape(mrn.value)}^^^{escape(self._authority(mrn))}^MR"
        return "|".join(["PID", "1", "", cx, "", xpn, "", born, sex])

    def _order_fields(self, accession: str, placer: str) -> tuple[str, str]:
        """(ORC segment, OBR-2 and OBR-3) with the accession where the
        profile reads it."""
        field = self.hospital.accession_field
        orc2 = accession if field == "ORC-2" else placer
        orc3 = accession if field == "ORC-3" else ""
        obr2 = accession if field == "OBR-2" else placer
        obr3 = accession if field == "OBR-3" else ""
        return f"{orc2}|{orc3}", f"{obr2}|{obr3}"

    def adt(self, event: str, patient: dict[str, Any], mrn: Mrn, visit: str) -> str:
        return (
            "\r".join(
                [
                    self._msh(f"ADT^{event}"),
                    f"EVN|{event}|{self.stamp}",
                    self.pid(patient, mrn),
                    "|".join(["PV1", "1", "O"] + [""] * 16 + [escape(visit)]),
                ]
            )
            + "\r"
        )

    def orm(self, patient: dict[str, Any], mrn: Mrn, accession: str) -> str:
        orc, obr = self._order_fields(escape(accession), f"PL{escape(accession)}")
        return (
            "\r".join(
                [
                    self._msh("ORM^O01"),
                    self.pid(patient, mrn),
                    f"ORC|NW|{orc}||||||{self.stamp}",
                    f"OBR|1|{obr}|LAB1^Synthetic lab panel^99HUB|R||{self.stamp}",
                ]
            )
            + "\r"
        )

    def oru(
        self,
        patient: dict[str, Any],
        mrn: Mrn,
        accession: str,
        *,
        procedure: str = "LAB1^Synthetic lab panel^99HUB",
        status: str = "F",
        issued: str = "",
        obx: list[str] | None = None,
    ) -> str:
        orc, obr = self._order_fields(escape(accession), f"PL{escape(accession)}")
        obr_fields = (
            ["OBR", "1", obr.split("|")[0], obr.split("|")[1], procedure, "R", "", self.stamp]
            + [""] * 14
            + [issued or self.stamp, "", "", status]
        )
        lines = (
            obx
            if obx is not None
            else [
                f"OBX|1|NM|NUM1^Synthetic measurement^99HUB|1|5.4|mmol/L^mmol/L^UCUM|3.5-5.5|N|||F"
                f"|||{self.stamp}",
                "OBX|2|TX|RPT^Report Text||Synthetic result from hl7bridge from-fhir.||||||F",
            ]
        )
        return (
            "\r".join(
                [
                    self._msh("ORU^R01"),
                    self.pid(patient, mrn),
                    f"ORC|RE|{orc}",
                    "|".join(obr_fields),
                    *lines,
                ]
            )
            + "\r"
        )


def patient_messages(
    client: HapiClient, patient_id: str, hospital: HospitalConfig, now: datetime
) -> tuple[dict[str, str], Mrn]:
    """The four sample messages for one Patient, by file name."""
    patient = client.read_by_id("Patient", patient_id)
    if patient is None:
        raise FhirPostError(404, "Patient", reason="no Patient with that id")
    mrn = pick_mrn(patient)
    r = Renderer(hospital, now)
    accession = f"ACC{r.now:%Y%m%d%H%M%S}"
    visit = f"V{r.now:%Y%m%d%H%M%S}"
    return {
        "01_adt_a04.hl7": r.adt("A04", patient, mrn, visit),
        "02_adt_a08.hl7": r.adt("A08", patient, mrn, visit),
        "03_orm_o01.hl7": r.orm(patient, mrn, accession),
        "04_oru_r01.hl7": r.oru(patient, mrn, accession),
    }, mrn


def _obx(n: int, obs: dict[str, Any]) -> str | None:
    """One OBX for an Observation, or None for a value type this renderer
    does not write."""
    coding = ((obs.get("code") or {}).get("coding") or [{}])[0]
    code = f"{escape(coding.get('code', ''))}^{escape(coding.get('display', ''))}^99HUB"
    status = _OBS_STATUS.get(str(obs.get("status", "")), "F")
    if "valueQuantity" in obs:
        q = obs["valueQuantity"]
        unit = escape(q.get("code") or q.get("unit") or "")
        return f"OBX|{n}|NM|{code}|1|{escape(q.get('value', ''))}|{unit}|||||{status}"
    if "valueString" in obs:
        return f"OBX|{n}|ST|{code}|1|{escape(obs['valueString'])}||||||{status}"
    if "valueCodeableConcept" in obs:
        c = ((obs["valueCodeableConcept"].get("coding")) or [{}])[0]
        value = f"{escape(c.get('code', ''))}^{escape(c.get('display', ''))}^99HUB"
        return f"OBX|{n}|CWE|{code}|1|{value}||||||{status}"
    return None


def report_message(
    client: HapiClient, report_id: str, hospital: HospitalConfig, now: datetime
) -> tuple[str, Mrn, int]:
    """A basic ORU^R01 for a DiagnosticReport and its Observations, and the
    number of results it left out (value types it does not write)."""
    report = client.read_by_id("DiagnosticReport", report_id)
    if report is None:
        raise FhirPostError(404, "DiagnosticReport", reason="no DiagnosticReport with that id")
    subject = str((report.get("subject") or {}).get("reference", ""))
    if not subject.startswith("Patient/"):
        raise UsageError("the DiagnosticReport has no Patient subject to put in PID.")
    patient = client.read_by_id("Patient", subject.split("/", 1)[1].split("/")[0])
    if patient is None:
        raise FhirPostError(404, "Patient", reason="the report's Patient is not on the server")
    mrn = pick_mrn(patient)
    idents = [i for i in report.get("identifier") or [] if isinstance(i, dict) and i.get("value")]
    if not idents:
        raise UsageError("the DiagnosticReport has no identifier to use as the accession.")
    accession = str(idents[0]["value"])
    lines: list[str] = []
    skipped = 0
    for ref in report.get("result") or []:
        target = str(ref.get("reference", ""))
        if not target.startswith("Observation/"):
            skipped += 1
            continue
        obs = client.read_by_id("Observation", target.split("/", 1)[1].split("/")[0])
        line = _obx(len(lines) + 1, obs) if obs else None
        if line is None:
            skipped += 1
            continue
        lines.append(line)
    if report.get("conclusion"):
        lines.append(
            f"OBX|{len(lines) + 1}|TX|RPT^Report Text||{escape(report['conclusion'])}||||||F"
        )
    coding = ((report.get("code") or {}).get("coding") or [{}])[0]
    procedure = f"{escape(coding.get('code', ''))}^{escape(coding.get('display', ''))}^99HUB"
    r = Renderer(hospital, now)
    issued = ""
    if report.get("issued"):
        try:
            issued = format_hl7_ts(
                datetime.fromisoformat(str(report["issued"])).astimezone(hospital.tzinfo),
                include_offset=hospital.timestamps_include_offset,
            )
        except ValueError:
            issued = ""
    text = r.oru(
        patient,
        mrn,
        accession,
        procedure=procedure,
        status=_REPORT_STATUS.get(str(report.get("status", "")), "F"),
        issued=issued,
        obx=lines,
    )
    return text, mrn, skipped


__all__ = ["Mrn", "Renderer", "escape", "patient_messages", "pick_mrn", "report_message"]
