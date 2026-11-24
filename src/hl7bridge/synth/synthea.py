"""Radiology HL7 v2 messages from Synthea FHIR R4 Bundles, offline.

``hl7bridge from-synthea PATH --hospital ID --out DIR`` reads Synthea
output (one Bundle file, or a folder of them) and writes, for every
ImagingStudy, the feed a radiology system would send: an ADT^A04 (A01 for
an inpatient encounter) once per visit, an ORM^O01 new order and an
ORU^R01 report. Only message types hl7bridge already accepts are written.
``index.json`` maps each study to its MRN, accession, visit and files.

PID is rendered by ``fromfhir.pid_segment``, so the MRN rules are those of
``from-fhir``: only an identifier under the profile's MRN systems, escaped,
and a Patient with none stops the run. Accession, placer and visit numbers
are short hashes of the MRN and the source resource, so they do not move
when the seed changes. The seed picks the order lead time, the report
delay and the boilerplate report lines, from a generator of its own per
study, so adding a patient changes no other patient's messages.

Input files are named by their position in sorted path order ("file 3"),
never by their name: Synthea names each file after its patient.

No clock, no network: the same input, seed and profile give the same
bytes. Everything is rendered in memory and checked before ``write_run``
writes a file, so a usage error leaves nothing behind; ``write_run`` then
writes into a new folder beside ``--out`` and renames it to ``--out`` in
one step, so a failed write leaves no partial output either. A study that cannot be
rendered as messages the listener accepts is skipped with a fixed reason
(never a value), and the run reports it. Errors name PATH and ``--out`` by
their option, never by the path, and the index names an ImagingStudy by
its UUID id or by its position ("file 3 entry 7"), never by a fullUrl or a
free-text id.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import random
import re
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import HospitalConfig
from ..demofiles import OwnedFiles, remove_owned
from ..errors import DemoDirError, UsageError
from ..fromfhir import Mrn, escape, mrn_systems, pick_mrn, pid_segment
from ..hl7time import format_hl7_ts
from ..v2fhir.common import TOKEN_SPECIALS
from .hl7build import message, msh, obr, seg

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_FILES = 20_000
MAX_STUDIES = 10_000
MAX_REPORT_TEXT_BYTES = 32 * 1024
MAX_REPORT_LINES = 500
MAX_MESSAGE_BYTES = 64 * 1024  # far under the listener's frame limit
INDEX_NAME = "index.json"
GENERATOR = "hl7bridge from-synthea"
PROCESSING_ID = "T"
NEEDED_TYPES = ("ADT^A04", "ORM^O01", "ORU^R01")
INPATIENT_EVENT = "ADT^A01"
YEARS = (1900, 2100)  # times outside are refused: no year below 1000 on any OS
BIRTH_YEARS = (1800, 2100)

SKIP_NO_PATIENT = "the ImagingStudy names no Patient in its Bundle"
SKIP_NO_ID = "the ImagingStudy has no id and no fullUrl to key it on"
SKIP_PATIENT_SHAPE = "the Patient's identifier, name or gender is not in FHIR shape"
SKIP_BIRTH_DATE = "the Patient's birthDate is not a full date from 1800 to 2100"
SKIP_UNSAFE_MRN = "the Patient's MRN is padded or holds a character a FHIR search key cannot"
SKIP_DUPLICATE = "the same ImagingStudy for the same MRN was already read"
SKIP_STARTED = "ImagingStudy.started is not a date and time with a UTC offset from 1900 to 2100"
SKIP_MODALITY = "the ImagingStudy has no DICOM modality code"
SKIP_CODE = "the procedure code is not a FHIR code"
SKIP_TIME = "a linked resource's time is not a date and time with a UTC offset from 1900 to 2100"
SKIP_ORDER = "the times are out of order (encounter, order, exam, report)"
SKIP_REPEATED_HOUR = "a time falls in a repeated local hour and the profile writes no UTC offset"
SKIP_CLASS = "the Encounter class has no HL7 table 0004 patient class"
SKIP_INPATIENT = "an inpatient Encounter, and the profile does not expect ADT^A01"
SKIP_REPORT_STATUS = "the linked DiagnosticReport's status has no ORU result status"
SKIP_REPORT_COUNT = "more than one DiagnosticReport names the ImagingStudy"
SKIP_REPORT_TEXT = (
    "the linked DiagnosticReport's text is unreadable, over 32 KiB or over 499 lines"
    " (500 with the synthetic line)"
)
SKIP_TOO_LARGE = "a rendered message would be over 64 KiB"
SKIP_OTHER_PATIENT = "a linked Encounter, ServiceRequest or DiagnosticReport names another Patient"
SKIP_OFFSET_SECONDS = (
    "a time's local UTC offset has seconds (before about 1972), which an HL7 offset cannot hold"
)

# Encounter.class (v3 ActCode) to PV1-2 (table 0004), the reverse of the
# IG map hl7bridge reads PV1-2 with; inpatient codes give an ADT^A01.
_PATIENT_CLASS = {
    "AMB": "O",
    "EMER": "E",
    "FLD": "O",
    "HH": "O",
    "IMP": "I",
    "ACUTE": "I",
    "NONAC": "I",
    "OBSENC": "O",
    "PRENC": "P",
    "SS": "O",
    "VR": "O",
}
_INPATIENT = frozenset({"I"})
# FHIR request-priority to table 0027. "urgent" has no 0027 code: left empty.
_PRIORITY = {"routine": "R", "asap": "A", "stat": "S"}
# DiagnosticReport.status to OBR-25. Only P, F and C are accepted by the
# listener; any other status skips the study.
_REPORT_STATUS = {
    "preliminary": "P",
    "partial": "P",
    "final": "F",
    "amended": "C",
    "corrected": "C",
    "appended": "C",
}
# Coding system URI to its HL7 table 0396 name in OBR-4.3; anything else is
# a local code (99 prefix).
_CODE_SYSTEMS = {
    "http://snomed.info/sct": "SCT",
    "http://loinc.org": "LN",
    "http://www.ama-assn.org/go/cpt": "C4",
}
LOCAL_CODE_SYSTEM = "99HUB"
_MODALITY = re.compile(r"[A-Z]{2,3}")
# A DICOM UID (PS3.5 9.1): two or more numeric components, no leading zeros.
_UID = re.compile(r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))+")
_DICOM_UID_SYSTEM = "urn:dicom:uid"
_URI_SYSTEM = "urn:ietf:rfc:3986"  # Synthea's: value urn:oid:<uid>
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_FHIR_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T.+")
_FULL_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_FHIR_CODE = re.compile(r"[^\s]+( [^\s]+)*")
_SEPARATORS = ("|", "^", "~", "\\", "&")

FINDINGS = (
    "FINDINGS: No acute abnormality.",
    "FINDINGS: No focal abnormality identified.",
    "FINDINGS: Unremarkable examination for age.",
    "FINDINGS: Normal appearance of the imaged structures.",
)
IMPRESSIONS = (
    "IMPRESSION: No acute findings.",
    "IMPRESSION: Normal examination.",
    "IMPRESSION: No significant abnormality.",
)
SYNTHETIC_LINE = "SYNTHETIC REPORT from Synthea data by hl7bridge. Not a real patient."
ORDER_LEAD_MINUTES = (15, 120)
REPORT_DELAY_MINUTES = (20, 240)


@dataclass(frozen=True, slots=True)
class Skip:
    """A study not written. ``imaging_study`` is the study's label, never
    a fullUrl or a free-text id."""

    source: str
    imaging_study: str
    reason: str


@dataclass(slots=True)
class SyntheaRun:
    """What one run would write. ``files`` maps a file name to its bytes,
    in feed order; ``index`` is the content of ``index.json``."""

    hospital_id: str
    seed: int
    bundles: int = 0
    studies: int = 0
    files: dict[str, bytes] = field(default_factory=dict)
    index: dict[str, Any] = field(default_factory=dict)
    skipped: list[Skip] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class _Study:
    source: str
    ref: str  # the raw reference, hashed into keys; never published
    label: str  # what the index and the skip list show
    position: tuple[int, int]  # (file number, entry number): read order
    patient: dict[str, Any]
    mrn: Mrn
    started: datetime
    modality: str
    code: str
    code_text: str
    code_system: str
    priority: str
    study_uid: str | None
    visit_key: str
    visit: str
    patient_class: str
    encounter_start: datetime | None
    authored: datetime | None
    report_status: str
    report_issued: datetime | None
    report_lines: list[str] | None

    @property
    def identity(self) -> tuple[str, str, str]:
        """One study of one patient: the MRN system and value, and the
        ImagingStudy reference. Two copies with one identity are one study."""
        return (self.mrn.system, self.mrn.value, self.ref)


@dataclass(slots=True)
class _Message:
    at: datetime
    rank: int
    seq: int
    kind: str
    text: str


class _StudySkipped(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# -- reading -------------------------------------------------------------------------------


def _is_link(path: Path) -> bool:
    """A symbolic link, or on Windows a directory junction (which
    ``is_symlink`` does not report)."""
    return path.is_symlink() or path.is_junction()


def _raise(exc: OSError) -> None:
    raise exc


def _json_files(root: Path) -> Iterator[Path]:
    """Every ``*.json`` file below ``root``. A linked folder (symlink or
    junction) is not entered, so nothing outside PATH is read: ``rglob``
    and ``os.walk`` both descend into a junction on Windows."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _is_link(base / d)]
        for name in filenames:
            candidate = base / name
            if candidate.suffix == ".json" and candidate.is_file():
                yield candidate


def bundle_files(path: Path) -> list[Path]:
    """``path`` itself when it is a file, else every ``*.json`` below it
    (the suffix matched exactly, on every OS), sorted by relative path.
    Errors name PATH, never the path: Synthea names files after patients."""
    if path.is_file():
        return [path]
    if not path.exists():
        raise UsageError("PATH does not exist.", hint="Point PATH at Synthea's output/fhir.")
    files = sorted(_json_files(path), key=lambda p: p.relative_to(path).as_posix())
    if not files:
        raise UsageError("no *.json files under PATH.", hint="Point PATH at output/fhir.")
    if len(files) > MAX_FILES:
        raise UsageError(
            f"{len(files)} files under PATH; the limit is {MAX_FILES}.",
            hint="Split the input into smaller folders.",
        )
    return files


def source_name(n: int) -> str:
    """How the index and errors name input file ``n`` (1-based, in sorted
    path order). Never the file name: Synthea names files after patients."""
    return f"file {n}"


def load_bundle(path: Path, source: str) -> dict[str, Any]:
    """One Bundle, read whole. Over MAX_BUNDLE_BYTES, not UTF-8 JSON (a
    lone surrogate escape included), or not a Bundle with a list of entries
    is a usage error naming the file by its position."""
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise UsageError(f"{source} is larger than {MAX_BUNDLE_BYTES} bytes.")
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise UsageError(f"{source} is not UTF-8 JSON.") from None
    if not isinstance(data, dict) or data.get("resourceType") != "Bundle":
        raise UsageError(f"{source} is not a FHIR Bundle.", hint="Point PATH at output/fhir.")
    entries = data.get("entry", [])
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("resource"), dict) for e in entries
    ):
        raise UsageError(f"{source}: Bundle.entry is not a list of entries with a resource.")
    return data


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


class _Refs:
    """Resolves references inside one Bundle: by ``fullUrl`` and by
    ``Type/id``. Nothing else is followed."""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self.by_ref: dict[str, dict[str, Any]] = {}
        self.resources: list[tuple[str, dict[str, Any]]] = []
        for entry in _list(bundle.get("entry")):
            resource = entry["resource"]
            kind = _str(resource.get("resourceType"))
            full_url = _str(entry.get("fullUrl"))
            self.resources.append((full_url, resource))
            if full_url:
                self.by_ref.setdefault(full_url, resource)
            rid = _str(resource.get("id"))
            if kind and rid:
                self.by_ref.setdefault(f"{kind}/{rid}", resource)

    def get(self, reference: Any, kind: str) -> dict[str, Any] | None:
        target = self.by_ref.get(_str(_obj(reference).get("reference")))
        if target is None or target.get("resourceType") != kind:
            return None
        return target

    def of_type(self, kind: str) -> list[tuple[int, str, dict[str, Any]]]:
        return [
            (n, url, r)
            for n, (url, r) in enumerate(self.resources)
            if r.get("resourceType") == kind
        ]


def _when(value: Any, reason: str = SKIP_TIME) -> datetime:
    """A FHIR dateTime with a time, an offset and a year in YEARS."""
    text = _str(value)
    if not _FHIR_DATETIME.fullmatch(text):
        raise _StudySkipped(reason)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _StudySkipped(reason) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _StudySkipped(reason)
    if not YEARS[0] <= parsed.year <= YEARS[1]:
        raise _StudySkipped(reason)
    return parsed


def _optional_when(value: Any) -> datetime | None:
    return None if value is None else _when(value)


def _coding(concept: Any) -> dict[str, Any]:
    return _obj(next(iter(_list(_obj(concept).get("coding"))), {}))


def _procedure(concept: Any, modality: str) -> tuple[str, str, str]:
    """(code, text, coding system name) for OBR-4, each escaped. With no
    code, a local code from the modality, so the order still has one."""
    coding = _coding(concept)
    raw_code = coding.get("code")
    code = escape(raw_code) if raw_code is not None else ""
    if raw_code is not None and not (
        isinstance(raw_code, str)
        and _FHIR_CODE.fullmatch(raw_code)
        and _FHIR_CODE.fullmatch(code)
        and not any(unicodedata.category(ch) == "Cc" for ch in raw_code)
    ):
        raise _StudySkipped(SKIP_CODE)
    text = escape(coding.get("display") or _obj(concept).get("text") or "")
    if not code:
        return f"IMG{modality}", text or f"{modality} imaging study", LOCAL_CODE_SYSTEM
    system = _CODE_SYSTEMS.get(_str(coding.get("system")), LOCAL_CODE_SYSTEM)
    return code, text, system


def _modality(study: dict[str, Any]) -> str:
    candidates = [_obj(m).get("code") for m in _list(study.get("modality"))]
    candidates += [_obj(_obj(s).get("modality")).get("code") for s in _list(study.get("series"))]
    for code in candidates:
        if isinstance(code, str) and _MODALITY.fullmatch(code):
            return code
    raise _StudySkipped(SKIP_MODALITY)


def _study_uid(study: dict[str, Any]) -> str | None:
    """The study instance UID, for the index only: from an identifier under
    ``urn:dicom:uid`` (value bare, or prefixed ``urn:oid:`` or
    ``urn:dicom:uid:``) or a ``urn:ietf:rfc:3986`` one whose value is
    ``urn:oid:<uid>`` (Synthea's form). An accession or any other
    identifier is never taken for it, however numeric."""
    for ident in _list(study.get("identifier")):
        ident = _obj(ident)
        system, value = _str(ident.get("system")), _str(ident.get("value"))
        if system == _DICOM_UID_SYSTEM:
            for prefix in ("urn:oid:", "urn:dicom:uid:"):
                if value.startswith(prefix):
                    value = value[len(prefix) :]
                    break
        elif system == _URI_SYSTEM and value.startswith("urn:oid:"):
            value = value[len("urn:oid:") :]
        else:
            continue
        if _UID.fullmatch(value) and len(value) <= 64:
            return value
    return None


def _report_text(report: dict[str, Any]) -> list[str]:
    """The linked report's lines, escaped. The cap leaves room for the
    synthetic line, so a report is never more than MAX_REPORT_LINES OBX."""
    conclusion = report.get("conclusion")
    if conclusion is not None and not isinstance(conclusion, str):
        raise _StudySkipped(SKIP_REPORT_TEXT)
    text = conclusion or ""
    if not text:
        for form in _list(report.get("presentedForm")):
            form = _obj(form)
            kind = form.get("contentType")
            if kind is not None and not isinstance(kind, str):
                raise _StudySkipped(SKIP_REPORT_TEXT)
            if _str(kind).startswith("text/plain") and form.get("data") is not None:
                data = form["data"]
                if not isinstance(data, str):  # a number, list or object is not base64
                    raise _StudySkipped(SKIP_REPORT_TEXT)
                if not data:  # an empty form: look at the next one
                    continue
                try:
                    text = base64.b64decode(data, validate=True).decode("utf-8")
                except (binascii.Error, ValueError):
                    raise _StudySkipped(SKIP_REPORT_TEXT) from None
                break
    if len(text.encode("utf-8")) > MAX_REPORT_TEXT_BYTES:
        raise _StudySkipped(SKIP_REPORT_TEXT)
    lines = [line for line in (escape(raw) for raw in text.splitlines()) if line]
    if len(lines) > MAX_REPORT_LINES - 1:  # the synthetic line is the last OBX
        raise _StudySkipped(SKIP_REPORT_TEXT)
    return lines


def _patient_shape_ok(patient: dict[str, Any]) -> bool:
    """The Patient elements ``pick_mrn`` and ``pid_segment`` read have
    their FHIR types, so a hand-edited file is a skip, never a crash."""
    idents = patient.get("identifier", [])
    if not isinstance(idents, list):
        return False
    for ident in idents:
        if not isinstance(ident, dict):
            return False
        if not all(isinstance(ident.get(k, ""), str) for k in ("system", "value")):
            return False
        kind = ident.get("type", {})
        if not isinstance(kind, dict) or not isinstance(kind.get("coding", []), list):
            return False
        if not all(isinstance(c, dict) for c in kind.get("coding", [])):
            return False
    names = patient.get("name", [])
    if not isinstance(names, list) or not all(isinstance(n, dict) for n in names):
        return False
    if names:
        given = names[0].get("given", [])
        if not isinstance(names[0].get("family", ""), str) or not isinstance(given, list):
            return False
        if not all(isinstance(g, str) for g in given):
            return False
    return isinstance(patient.get("gender", ""), str)


def _birth_date_ok(patient: dict[str, Any]) -> bool:
    """No ``birthDate`` at all (PID-7 empty), or a full date in BIRTH_YEARS.
    JSON null is not absent: it is skipped, never written as "None"."""
    if "birthDate" not in patient:
        return True
    value = patient["birthDate"]
    if not isinstance(value, str) or not _FULL_DATE.fullmatch(value):
        return False
    try:
        born = date.fromisoformat(value)
    except ValueError:
        return False
    return BIRTH_YEARS[0] <= born.year <= BIRTH_YEARS[1]


def _unsafe_key(value: str) -> bool:
    return (
        not value.strip()
        or value != value.strip()
        or any(ch in value for ch in TOKEN_SPECIALS)
        or any(unicodedata.category(ch) == "Cc" for ch in value)
    )


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest().upper()


# -- planning ------------------------------------------------------------------------------


class _Planner:
    def __init__(self, hospital: HospitalConfig, seed: int) -> None:
        self.hospital = hospital
        self.seed = seed
        self.systems = mrn_systems(hospital)
        self.studies: list[_Study] = []
        # (read position, skip): sorted by position when the run is done
        self.skipped: list[tuple[tuple[int, int], Skip]] = []
        self.total = 0

    def skip(self, position: tuple[int, int], source: str, label: str, reason: str) -> None:
        self.skipped.append((position, Skip(source, label, reason)))

    def read(self, bundle: dict[str, Any], source: str, file_no: int) -> None:
        refs = _Refs(bundle)
        reports: dict[int, list[dict[str, Any]]] = {}
        for _, _, report in refs.of_type("DiagnosticReport"):
            targets = [refs.get(r, "ImagingStudy") for r in _list(report.get("imagingStudy"))]
            targets += [refs.get(r, "ServiceRequest") for r in _list(report.get("basedOn"))]
            for target in {id(t): t for t in targets if t is not None}:
                reports.setdefault(target, []).append(report)
        for n, full_url, study in refs.of_type("ImagingStudy"):
            self.total += 1
            if self.total > MAX_STUDIES:
                raise UsageError(
                    f"more than {MAX_STUDIES} imaging studies; the limit is {MAX_STUDIES}.",
                    hint="Split the input into smaller folders.",
                )
            position = (file_no, n + 1)
            rid = _str(study.get("id"))
            # The index shows a UUID id (Synthea's), else the position: a
            # fullUrl can be a file URL and an id can be free text.
            label = f"ImagingStudy/{rid}" if _UUID.fullmatch(rid) else f"{source} entry {n + 1}"
            if not (rid or full_url):  # keys would move with the file's position
                self.skip(position, source, label, SKIP_NO_ID)
                continue
            ref = f"ImagingStudy/{rid}" if rid else full_url
            try:
                planned = self._plan(refs, reports, study, source, ref, label, position)
            except _StudySkipped as skip:
                self.skip(position, source, label, skip.reason)
                continue
            self.studies.append(planned)

    def _patient(
        self, refs: _Refs, study: dict[str, Any], source: str
    ) -> tuple[dict[str, Any], Mrn]:
        patient = refs.get(study.get("subject"), "Patient")
        if patient is None:
            raise _StudySkipped(SKIP_NO_PATIENT)
        if not _patient_shape_ok(patient):
            raise _StudySkipped(SKIP_PATIENT_SHAPE)
        if not _birth_date_ok(patient):
            raise _StudySkipped(SKIP_BIRTH_DATE)
        try:
            mrn = pick_mrn(patient, self.systems)
        except UsageError as exc:
            raise UsageError(f"{source}: {exc}", hint=exc.hint) from None
        if _unsafe_key(mrn.value):
            raise _StudySkipped(SKIP_UNSAFE_MRN)
        return patient, mrn

    @staticmethod
    def _same_patient(refs: _Refs, linked: dict[str, Any], patient: dict[str, Any]) -> None:
        """A linked resource with a ``subject`` must name the study's own
        Patient (by fullUrl or Type/id), else the study is skipped: its
        visit, order or report text belongs to someone else."""
        if "subject" in linked and refs.get(linked["subject"], "Patient") is not patient:
            raise _StudySkipped(SKIP_OTHER_PATIENT)

    def _plan(
        self,
        refs: _Refs,
        reports: dict[int, list[dict[str, Any]]],
        study: dict[str, Any],
        source: str,
        ref: str,
        label: str,
        position: tuple[int, int],
    ) -> _Study:
        patient, mrn = self._patient(refs, study, source)
        started = _when(study.get("started"), SKIP_STARTED)
        modality = _modality(study)

        encounter = refs.get(study.get("encounter"), "Encounter")
        patient_class = "O"
        encounter_start = None
        # No Encounter: the study is its own outpatient visit. The direct
        # listener writes nothing for an ADT with no PV1-19 and answers AE.
        visit_of = f"study|{ref}"
        if encounter is not None:
            self._same_patient(refs, encounter, patient)
            code = _str(_obj(encounter.get("class")).get("code"))
            if code not in _PATIENT_CLASS:
                raise _StudySkipped(SKIP_CLASS)
            patient_class = _PATIENT_CLASS[code]
            if patient_class in _INPATIENT and (
                INPATIENT_EVENT not in self.hospital.expected_message_types
            ):
                raise _StudySkipped(SKIP_INPATIENT)
            encounter_start = _optional_when(_obj(encounter.get("period")).get("start"))
            enc_id = _str(encounter.get("id"))
            visit_of = f"Encounter/{enc_id}" if enc_id else str(study["encounter"]["reference"])
        visit = "V" + _hash("visit", mrn.system, mrn.value, visit_of)[:15]

        order = None
        for based_on in _list(study.get("basedOn")):
            order = refs.get(based_on, "ServiceRequest")
            if order is not None:
                break
        concept = study["procedureCode"][0] if _list(study.get("procedureCode")) else None
        priority = ""
        authored = None
        linked = list(reports.get(id(study), []))
        if order is not None:
            self._same_patient(refs, order, patient)
            order_code = _obj(order.get("code"))
            if _list(order_code.get("coding")) or order_code.get("text"):
                concept = order_code
            priority = _PRIORITY.get(_str(order.get("priority")), "")
            authored = _optional_when(order.get("authoredOn"))
            linked += [r for r in reports.get(id(order), []) if r not in linked]
        code, code_text, code_system = _procedure(concept, modality)

        for report in linked:
            self._same_patient(refs, report, patient)
        status, issued, lines = "F", None, None
        if len(linked) > 1:
            raise _StudySkipped(SKIP_REPORT_COUNT)
        if linked:
            report = linked[0]
            if _str(report.get("status")) not in _REPORT_STATUS:
                raise _StudySkipped(SKIP_REPORT_STATUS)
            status = _REPORT_STATUS[_str(report.get("status"))]
            issued = _optional_when(report.get("issued"))
            lines = _report_text(report) or None

        if (
            (encounter_start is not None and encounter_start > started)
            or (authored is not None and authored > started)
            or (issued is not None and issued < started)
        ):
            raise _StudySkipped(SKIP_ORDER)
        return _Study(
            source=source,
            ref=ref,
            label=label,
            position=position,
            patient=patient,
            mrn=mrn,
            started=started,
            modality=modality,
            code=code,
            code_text=code_text,
            code_system=code_system,
            priority=priority,
            study_uid=_study_uid(study),
            visit_key="\x1f".join((mrn.system, mrn.value, visit_of)),
            visit=visit,
            patient_class=patient_class,
            encounter_start=encounter_start,
            authored=authored,
            report_status=status,
            report_issued=issued,
            report_lines=lines,
        )


# -- rendering -----------------------------------------------------------------------------


@dataclass(slots=True)
class _Timed:
    study: _Study
    seq: int
    order_at: datetime
    report_at: datetime
    lines: list[str]


@dataclass(slots=True)
class _Rendered:
    timed: _Timed
    accession: str
    orm: _Message
    oru: _Message


class _Writer:
    def __init__(self, hospital: HospitalConfig, seed: int) -> None:
        self.hospital = hospital
        self.seed = seed

    def local(self, at: datetime) -> datetime:
        return at.astimezone(self.hospital.tzinfo)

    def ts(self, at: datetime) -> str:
        return format_hl7_ts(self.local(at), include_offset=self.hospital.timestamps_include_offset)

    def ambiguous(self, at: datetime) -> bool:
        """True when ``at`` falls in a repeated local hour: written with no
        offset, the listener would read it as the earlier one."""
        local = self.local(at)
        return local.replace(fold=1 - local.fold).utcoffset() != local.utcoffset()

    def offset_has_seconds(self, at: datetime) -> bool:
        """True when the local UTC offset at ``at`` is not whole minutes
        (local mean time before about 1972). An HL7 offset is +HHMM, so
        writing it would move the instant by those seconds."""
        offset = self.local(at).utcoffset()
        return offset is not None and offset.total_seconds() % 60 != 0

    def finish(self, message_type: str, app: str, at: datetime, s: _Study, *body: str) -> str:
        """The message: MSH, then ``body``. MSH-10 hashes the seed, the
        study and the whole message with MSH-10 left empty (every other MSH
        field included), so a changed message never reuses a control id."""
        h = self.hospital

        def header(control: str) -> str:
            return msh(
                sending_app=app,
                sending_facility=h.facility_codes[0],
                receiving_app="HL7BRIDGE",
                receiving_facility="HUB",
                ts=self.local(at),
                include_offset=h.timestamps_include_offset,
                message_type=message_type,
                control_id=control,
                version=h.hl7_version,
            )

        draft = message(header(""), *body)
        control = "S" + _hash(str(self.seed), message_type, s.ref, draft)[:19]
        return message(header(control), *body)

    def pid(self, s: _Study) -> str:
        return pid_segment(self.hospital, s.patient, s.mrn)

    def pv1(self, s: _Study, admitted: datetime | None) -> str:
        fields = ["PV1", "1", s.patient_class] + [""] * 42
        fields[19] = s.visit
        if admitted is not None:
            fields[44] = self.ts(admitted)
        return seg(*fields).rstrip("|")

    def order_numbers(self, accession: str, placer: str) -> tuple[str, str]:
        """(placer number, filler number), the same in ORC and OBR. The
        accession goes where the profile reads it: when that is ORC-2 or
        OBR-2 it is the placer number in both, when ORC-3 or OBR-3 the
        filler number in both. Else the placer is the hashed one and the
        filler empty."""
        where = self.hospital.accession_field
        placer_no = accession if where in ("ORC-2", "OBR-2") else placer
        filler_no = accession if where in ("ORC-3", "OBR-3") else ""
        return placer_no, filler_no

    def adt(self, s: _Study, event: str, at: datetime) -> str:
        trigger = event.split("^")[1]
        return self.finish(
            event,
            "ADT",
            at,
            s,
            seg("EVN", trigger, self.ts(at)),
            self.pid(s),
            self.pv1(s, s.encounter_start),
        )

    def _obr(self, s: _Study, placer_no: str, filler_no: str, **times: str) -> str:
        return obr(
            placer=placer_no,
            filler=filler_no,
            procedure_code=s.code,
            procedure_desc=s.code_text,
            code_system=s.code_system,
            priority=s.priority,
            modality=s.modality,
            **times,
        )

    def orm(self, s: _Study, accession: str, placer: str, at: datetime) -> str:
        placer_no, filler_no = self.order_numbers(accession, placer)
        return self.finish(
            "ORM^O01",
            "RIS",
            at,
            s,
            self.pid(s),
            self.pv1(s, None),
            seg("ORC", "NW", placer_no, filler_no, "", "", "", "", "", self.ts(at)),
            self._obr(s, placer_no, filler_no).rstrip("|"),
        )

    def oru(self, s: _Study, accession: str, placer: str, at: datetime, lines: list[str]) -> str:
        placer_no, filler_no = self.order_numbers(accession, placer)
        status = s.report_status
        obx = [
            seg("OBX", str(n), "TX", "RPT^Report Text", "", line, "", "", "", "", "", status)
            for n, line in enumerate(lines, start=1)
        ]
        return self.finish(
            "ORU^R01",
            "RIS",
            at,
            s,
            self.pid(s),
            self.pv1(s, None),
            seg("ORC", "RE", placer_no, filler_no),
            self._obr(
                s,
                placer_no,
                filler_no,
                observed=self.ts(s.started),
                result_ts=self.ts(at),
                status=status,
            ),
            *obx,
        )


def _time(s: _Study, seq: int, seed: int, writer: _Writer) -> _Timed:
    """Seeded times and report lines for one study, or a skip when its
    times cannot be written as the same instants."""
    rng = random.Random("\x1f".join((str(seed), *s.identity)))
    lead = rng.randint(*ORDER_LEAD_MINUTES)
    delay = rng.randint(*REPORT_DELAY_MINUTES)
    findings = rng.choice(FINDINGS)
    impression = rng.choice(IMPRESSIONS)

    order_at = s.authored
    if order_at is None:
        order_at = s.started - timedelta(minutes=lead)
        if s.encounter_start is not None:
            order_at = max(order_at, s.encounter_start)
    report_at = s.report_issued or s.started + timedelta(minutes=delay)
    times = [t for t in (s.encounter_start, order_at, s.started, report_at) if t is not None]
    if writer.hospital.timestamps_include_offset:
        if any(writer.offset_has_seconds(t) for t in times):
            raise _StudySkipped(SKIP_OFFSET_SECONDS)
    elif any(writer.ambiguous(t) for t in times):
        raise _StudySkipped(SKIP_REPEATED_HOUR)
    lines = s.report_lines or [f"EXAM: {s.code_text}", findings, impression]
    return _Timed(s, seq, order_at, report_at, [*lines, SYNTHETIC_LINE])


def _render_one(s: _Study, seq: int, seed: int, writer: _Writer) -> _Rendered:
    t = _time(s, seq, seed, writer)
    accession = "A" + _hash("accession", *s.identity)[:15]
    placer = "P" + _hash("placer", *s.identity)[:15]
    orm_text = writer.orm(s, accession, placer, t.order_at)
    oru_text = writer.oru(s, accession, placer, t.report_at, t.lines)
    adt_text = writer.adt(s, "ADT^A04", t.order_at)  # the same size as the one written
    if any(len(x.encode("utf-8")) > MAX_MESSAGE_BYTES for x in (orm_text, oru_text, adt_text)):
        raise _StudySkipped(SKIP_TOO_LARGE)
    orm = _Message(t.order_at, 1, seq, "ORM^O01", orm_text)
    oru = _Message(t.report_at, 2, seq, "ORU^R01", oru_text)
    return _Rendered(t, accession, orm, oru)


def _render(planner: _Planner, run: SyntheaRun) -> None:
    hospital = planner.hospital
    writer = _Writer(hospital, planner.seed)
    # seq breaks ties in feed order: oldest exam first, then read order.
    by_exam = sorted(planner.studies, key=lambda s: (s.started, s.position))
    seqs = {id(s): seq for seq, s in enumerate(by_exam)}

    # Copies of one study (the same MRN and ImagingStudy) are rendered in
    # read order and the first that renders is written; a copy skipped for
    # its own reason never makes a later good copy a duplicate.
    rendered: list[_Rendered] = []
    written: set[tuple[str, str, str]] = set()
    for s in planner.studies:
        try:
            one = _render_one(s, seqs[id(s)], planner.seed, writer)
        except _StudySkipped as skip:
            planner.skip(s.position, s.source, s.label, skip.reason)
            continue
        if s.identity in written:
            planner.skip(s.position, s.source, s.label, SKIP_DUPLICATE)
            continue
        written.add(s.identity)
        rendered.append(one)
    rendered.sort(key=lambda r: r.timed.seq)

    # A visit's ADT comes before every order in it: the earliest of the
    # encounter start and the order times of all its written studies.
    visit_at: dict[str, datetime] = {}
    for r in rendered:
        t = r.timed
        first = min(x for x in (t.study.encounter_start, t.order_at) if x is not None)
        key = t.study.visit_key
        visit_at[key] = min(visit_at.get(key, first), first)

    messages: list[_Message] = []
    entries: list[tuple[dict[str, Any], _Message, _Message, str]] = []
    visits: dict[str, _Message] = {}
    for r in rendered:
        t, s = r.timed, r.timed.study
        if s.visit_key not in visits:
            event = INPATIENT_EVENT if s.patient_class in _INPATIENT else "ADT^A04"
            at = visit_at[s.visit_key]
            adt = _Message(at, 0, t.seq, event, writer.adt(s, event, at))
            visits[s.visit_key] = adt
            messages.append(adt)
        messages += [r.orm, r.oru]
        entry = {
            "source": s.source,
            "imaging_study": s.label,
            "study_uid": s.study_uid,
            "mrn": s.mrn.value,
            "mrn_system": s.mrn.system,
            "accession": r.accession,
            "visit": s.visit,
            "modality": s.modality,
            "report_status": s.report_status,
            "report_text": "source" if s.report_lines else "generated",
        }
        entries.append((entry, r.orm, r.oru, s.visit_key))

    messages.sort(key=lambda m: (m.at, m.rank, m.seq))
    names: dict[int, str] = {}
    for n, m in enumerate(messages, start=1):
        name = f"{n:05d}_{m.kind.replace('^', '_').lower()}.hl7"
        names[id(m)] = name
        run.files[name] = m.text.encode("utf-8")
        run.counts[m.kind] += 1
    studies = []
    for entry, orm, oru, visit_key in entries:
        entry["adt_file"] = names[id(visits[visit_key])]
        entry["order_file"] = names[id(orm)]
        entry["report_file"] = names[id(oru)]
        studies.append(entry)
    run.skipped = [skip for _, skip in sorted(planner.skipped, key=lambda p: p[0])]
    run.index = {
        "generator": GENERATOR,
        "hospital": hospital.hospital_id,
        "seed": planner.seed,
        "bundles": run.bundles,
        "counts": dict(sorted(run.counts.items())),
        "studies": studies,
        "skipped": [
            {"source": k.source, "imaging_study": k.imaging_study, "reason": k.reason}
            for k in run.skipped
        ],
    }
    run.files[INDEX_NAME] = (json.dumps(run.index, indent=2, sort_keys=True) + "\n").encode()


# -- the run -------------------------------------------------------------------------------


def _plain(value: str) -> bool:
    return not any(ch in value for ch in _SEPARATORS) and not any(
        unicodedata.category(ch) == "Cc" for ch in value
    )


def _authority_reads_back(authority: str) -> bool:
    """The direct listener reads PID-3.4 unescaped and splits it on ``&``
    (the subcomponent separator), and edge spaces do not survive, so such
    an authority never finds its MRN system again."""
    return (
        "&" not in authority
        and authority == authority.strip()
        and not any(unicodedata.category(ch) == "Cc" for ch in authority)
    )


def check_profile(hospital: HospitalConfig) -> None:
    """The profile can receive what this writes: the three message types,
    MSH-11 T in direct mode, an MSH-4 and MSH-12 with no separator, and
    MRN authorities the direct listener reads back from PID-3.4."""
    missing = [t for t in NEEDED_TYPES if t not in hospital.expected_message_types]
    if missing:
        raise UsageError(
            f"profile {hospital.hospital_id} does not expect {', '.join(missing)};"
            " the listener would refuse those messages.",
            hint="Add them to expected_message_types, or pick another --hospital.",
        )
    if PROCESSING_ID not in hospital.fhir.allowed_processing_ids:
        raise UsageError(
            f"profile {hospital.hospital_id} does not allow processing id {PROCESSING_ID};"
            " from-synthea writes MSH-11 T and the direct listener would refuse it.",
            hint="Add T to fhir.allowed_processing_ids, or pick another --hospital.",
        )
    if not (_plain(hospital.facility_codes[0]) and _plain(hospital.hl7_version)):
        raise UsageError(
            f"profile {hospital.hospital_id}: the first facility code or hl7_version holds"
            " an HL7 separator; it cannot go into MSH."
        )
    if not all(_authority_reads_back(a) for a, _ in hospital.fhir.mrn_by_authority):
        raise UsageError(
            f"profile {hospital.hospital_id}: an mrn_by_authority authority holds '&', a"
            " control character or edge spaces; the direct listener would not read it back"
            " from PID-3.4.",
            hint="Rename that authority in the profile.",
        )


def _refuse_link(out: Path) -> None:
    if _is_link(out):
        raise UsageError(
            "--out is a link; from-synthea writes only a new real folder.",
            hint="Pick a new folder.",
        )


def _check_out_folder(out: Path) -> None:
    """``--out`` must not exist yet: the run is written beside it and
    renamed into place in one step, so a folder that is already there
    (empty or not, or a link or junction) is refused."""
    _refuse_link(out)
    if out.exists() and not out.is_dir():
        raise UsageError("--out exists and is not a folder.", hint="Pick a new folder.")
    if out.is_dir() and any(out.iterdir()):
        raise UsageError(
            "--out is not empty; from-synthea never mixes two runs.", hint="Pick a new folder."
        )
    if out.is_dir():
        raise UsageError(
            "--out already exists; from-synthea writes a new folder, so a failed run"
            " leaves nothing in it.",
            hint="Pick a name that does not exist yet.",
        )


def check_out(path: Path, out: Path) -> None:
    """Before any Bundle is read: ``--out`` is not a link, is not inside
    PATH (the next run would read ``index.json`` as a Bundle), with links
    and junctions on the way resolved, and does not exist yet. Neither path
    is named: either can carry a patient's name."""
    _refuse_link(out)
    if path.is_dir():
        target, root = out.resolve(), path.resolve()
        if target == root or target.is_relative_to(root):
            raise UsageError(
                "--out is inside PATH; the next run would read its index.json as a Bundle.",
                hint="Pick a folder outside PATH.",
            )
    _check_out_folder(out)


def generate_from_synthea(path: Path, hospital: HospitalConfig, *, seed: int) -> SyntheaRun:
    """Read every Bundle under ``path`` and render the run. Writes nothing."""
    check_profile(hospital)
    files = bundle_files(path)
    planner = _Planner(hospital, seed)
    run = SyntheaRun(hospital_id=hospital.hospital_id, seed=seed)
    for n, file in enumerate(files, start=1):
        source = source_name(n)
        planner.read(load_bundle(file, source), source, n)
        run.bundles += 1
    if planner.total == 0:
        raise UsageError("no ImagingStudy in the Bundles under PATH; nothing to write.")
    run.studies = planner.total
    _render(planner, run)
    return run


STAGE_PREFIX = ".from-synthea-"


def _default_modes(folder: Path, names: list[str]) -> None:
    """Linux and macOS: the modes a plain ``mkdir`` and ``open`` would give
    under the umask, not ``mkdtemp``'s 0700 and the 0600 files, so another
    account (a listener, a container's host user) can read the output as
    before. Windows keeps the owner-only ACL ``mkdtemp`` sets."""
    if os.name == "nt":
        return
    mask = os.umask(0)
    os.umask(mask)
    for name in names:
        os.chmod(folder / name, 0o666 & ~mask)
    os.chmod(folder, 0o777 & ~mask)


def write_run(run: SyntheaRun, out: Path) -> None:
    """Write the run as the new folder ``out``.

    Every file is written, created exclusively, into a folder that
    ``tempfile.mkdtemp`` makes beside ``out``; when all are written that
    folder is renamed to ``out`` in one step. A failure before the rename
    removes the staged files and folder through ``demofiles.remove_owned``
    (the one deleting code in hl7bridge), so nothing is left in ``out``. A
    folder another writer makes at ``out`` meanwhile is never written into:
    the rename fails (or, on Linux and macOS, replaces it only if it is
    still empty)."""
    _check_out_folder(out)
    parent = out.absolute().parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=parent))
    except OSError:
        raise UsageError(
            "cannot make a new folder beside --out; nothing was written.",
            hint="Check that the folder that holds --out can be written to.",
        ) from None
    try:
        owned = OwnedFiles(stage, temporary=True)
    except (OSError, DemoDirError):
        # The empty stage cannot be removed without its identity; it stays.
        raise UsageError(
            "cannot use the new folder made beside --out; nothing was written.",
            hint="Check that the folder that holds --out can be written to.",
        ) from None
    published = False
    try:
        for name, data in run.files.items():
            owned.write_bytes(owned.root / name, data, stamp=False)
        _default_modes(owned.root, list(run.files))
        try:
            os.rename(owned.root, out)
        except OSError:
            if os.path.lexists(out):
                raise UsageError(
                    "--out was created while writing; nothing was written.",
                    hint="Pick a new folder.",
                ) from None
            raise
        published = True
    finally:
        if not published:
            remove_owned(owned)


__all__ = [
    "INDEX_NAME",
    "MAX_BUNDLE_BYTES",
    "MAX_FILES",
    "MAX_MESSAGE_BYTES",
    "MAX_REPORT_LINES",
    "MAX_REPORT_TEXT_BYTES",
    "MAX_STUDIES",
    "Skip",
    "SyntheaRun",
    "bundle_files",
    "check_out",
    "check_profile",
    "generate_from_synthea",
    "load_bundle",
    "source_name",
    "write_run",
]
