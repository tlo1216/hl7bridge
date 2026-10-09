"""Ground-truth manifest for generated data, plus a verifier that re-reads
the generated artifacts and confirms every seeded defect is actually present
(and nothing in the manifest is stale). Tests and the demo both rely on it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydicom import dcmread

from ..config import HospitalRegistry
from ..errors import HL7ParseError, TimestampError
from ..hl7time import parse_hl7_ts
from ..ingest.parser import (
    get_control_id,
    get_facilities,
    get_field,
    get_message_type,
    parse_hl7,
)

CATEGORIES = (
    "order_without_image",
    "image_without_order",
    "mismatch_patient",
    "mismatch_modality",
    "cancelled_order",
    "corrected_report",
    "missing_accession",
    "bad_timestamp",
    "parse_error",
    "unknown_hospital",
    "unexpected_message_type",
    "ack_errored",
    "ack_rejected",
    "ack_missing",
    "ack_duplicate",
    "ack_unknown_control",
    "unmerged_duplicate",
)


@dataclass(frozen=True, slots=True)
class Defect:
    id: str
    category: str
    hospital_id: str | None
    accession: str | None = None
    control_id: str | None = None
    mrns: tuple[str, ...] = ()
    note: str = ""


@dataclass(slots=True)
class Manifest:
    seed: int
    base_date: str
    report_now_utc: str
    hospitals: list[str]
    defects: list[Defect] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)

    def add(self, defect: Defect) -> None:
        if defect.category not in CATEGORIES:
            raise ValueError(f"unknown defect category {defect.category!r}")
        if any(d.id == defect.id for d in self.defects):
            raise ValueError(f"duplicate defect id {defect.id!r}")
        self.defects.append(defect)

    def by_category(self, category: str) -> list[Defect]:
        return [d for d in self.defects if d.category == category]

    def save(self, path: Path) -> None:
        payload = {
            "seed": self.seed,
            "base_date": self.base_date,
            "report_now_utc": self.report_now_utc,
            "hospitals": self.hospitals,
            "defects": [asdict(d) for d in self.defects],
            "expected": self.expected,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> Manifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = Manifest(
        seed=payload["seed"],
        base_date=payload["base_date"],
        report_now_utc=payload["report_now_utc"],
        hospitals=list(payload["hospitals"]),
        expected=payload["expected"],
    )
    for d in payload["defects"]:
        manifest.add(
            Defect(
                id=d["id"],
                category=d["category"],
                hospital_id=d["hospital_id"],
                accession=d.get("accession"),
                control_id=d.get("control_id"),
                mrns=tuple(d.get("mrns", ())),
                note=d.get("note", ""),
            )
        )
    return manifest


# -- verifier ----------------------------------------------------------------


@dataclass(slots=True)
class _Index:
    """Parsed view of everything the generator wrote."""

    # (hospital_dir, control_id) -> parsed message info
    parsed: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    unparseable: list[tuple[str, str]] = field(default_factory=list)  # (hospital_dir, filename)
    orm_accessions: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    oru_status_by_accession: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    acks_by_msa: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    adt_patients: dict[str, list[tuple[str, str, str, str]]] = field(default_factory=dict)
    merged_pairs: set[tuple[str, str, str]] = field(default_factory=set)
    cancelled_accessions: set[tuple[str, str]] = field(default_factory=set)
    studies: dict[tuple[str, str], list[dict[str, str]]] = field(default_factory=dict)
    outbound_controls: dict[str, set[str]] = field(default_factory=dict)


def _index_output(out_dir: Path, registry: HospitalRegistry) -> _Index:
    idx = _Index()
    for msg_path in sorted((out_dir / "messages").glob("*/*.hl7")):
        hosp_dir = msg_path.parent.name
        text = msg_path.read_text(encoding="utf-8")
        try:
            m = parse_hl7(text)
        except HL7ParseError:
            idx.unparseable.append((hosp_dir, msg_path.name))
            continue
        control = get_control_id(m)
        msg_type = get_message_type(m)
        msh4, msh6 = get_facilities(m)
        info = {"type": msg_type, "msh4": msh4, "msh6": msh6, "file": msg_path.name}
        idx.parsed[(hosp_dir, control)] = info

        hospital = registry.match_facility(msh4, msh6)
        if hospital is None:
            continue
        hosp = hospital.hospital_id
        accession = get_field(m, hospital.accession_field)
        if msg_type == "ORM^O01":
            info["accession"] = accession
            info["orc1"] = get_field(m, "ORC-1")
            info["modality"] = get_field(m, "OBR-24")
            info["mrn"] = get_field(m, "PID-3")
            if accession:
                if get_field(m, "ORC-1") == "CA":
                    idx.cancelled_accessions.add((hosp, accession))
                else:
                    idx.orm_accessions[(hosp, accession)] = info
        elif msg_type == "ORU^R01":
            info["accession"] = accession
            info["obr22"] = get_field(m, "OBR-22")
            info["status"] = get_field(m, "OBR-25")
            if accession:
                idx.oru_status_by_accession.setdefault((hosp, accession), set()).add(
                    get_field(m, "OBR-25")
                )
        elif msg_type.startswith("ACK"):
            idx.acks_by_msa.setdefault((hosp, get_field(m, "MSA-2")), []).append(
                get_field(m, "MSA-1")
            )
        elif msg_type.startswith("ADT^"):
            idx.adt_patients.setdefault(hosp, []).append(
                (
                    get_field(m, "PID-3"),
                    get_field(m, "PID-5", component=1),
                    get_field(m, "PID-5", component=2),
                    get_field(m, "PID-7"),
                )
            )
            if msg_type == "ADT^A40":
                idx.merged_pairs.add((hosp, get_field(m, "PID-3"), get_field(m, "MRG-1")))

    for dcm_path in sorted((out_dir / "dicom").glob("*/*.dcm")):
        hosp = dcm_path.parent.name
        ds = dcmread(dcm_path, stop_before_pixels=True)
        accession = str(getattr(ds, "AccessionNumber", "") or "")
        idx.studies.setdefault((hosp, accession), []).append(
            {
                "uid": str(ds.StudyInstanceUID),
                "modality": str(ds.Modality),
                "mrn": str(ds.PatientID),
                "file": dcm_path.name,
            }
        )

    outbound_path = out_dir / "outbound_reports.jsonl"
    if outbound_path.exists():
        for line in outbound_path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            idx.outbound_controls.setdefault(entry["hospital_id"], set()).add(entry["control_id"])
    return idx


def verify_manifest(out_dir: Path, registry: HospitalRegistry) -> list[str]:
    """Return a list of problems; empty means every seeded defect is present
    exactly as the manifest claims."""
    manifest = load_manifest(out_dir / "manifest.json")
    idx = _index_output(out_dir, registry)
    problems: list[str] = []

    def check(condition: bool, defect: Defect, why: str) -> None:
        if not condition:
            problems.append(f"{defect.id}: {why}")

    for d in manifest.defects:
        hosp = d.hospital_id or ""
        key = (hosp, d.accession or "")
        if d.category == "order_without_image":
            check(key in idx.orm_accessions, d, "seeded ORM not found")
            check(key not in idx.studies, d, "unexpected study exists")
        elif d.category == "image_without_order":
            check(key in idx.studies, d, "seeded study not found")
            check(key not in idx.orm_accessions, d, "unexpected ORM exists")
        elif d.category == "mismatch_patient":
            orm = idx.orm_accessions.get(key)
            studies = idx.studies.get(key, [])
            check(bool(orm) and bool(studies), d, "order or study missing")
            if orm and studies:
                check(studies[0]["mrn"] != orm["mrn"], d, "MRNs unexpectedly agree")
        elif d.category == "mismatch_modality":
            orm = idx.orm_accessions.get(key)
            studies = idx.studies.get(key, [])
            check(bool(orm) and bool(studies), d, "order or study missing")
            if orm and studies:
                check(studies[0]["modality"] != orm["modality"], d, "modalities unexpectedly agree")
        elif d.category == "cancelled_order":
            check(key in idx.cancelled_accessions, d, "no ORM^O01 CA found")
            check(key not in idx.studies, d, "cancelled order unexpectedly has a study")
        elif d.category == "corrected_report":
            statuses = idx.oru_status_by_accession.get(key, set())
            check("C" in statuses and "F" in statuses, d, f"need F and C ORUs, got {statuses}")
        elif d.category == "missing_accession":
            info = idx.parsed.get((hosp, d.control_id or ""))
            check(info is not None, d, "seeded message not found")
            if info is not None:
                check(
                    info.get("type") == "ORM^O01",
                    d,
                    f"expected ORM^O01, got {info.get('type')!r}",
                )
                check(
                    "accession" in info,
                    d,
                    "message not indexed against a configured hospital",
                )
                check(
                    info.get("accession", "unset") == "",
                    d,
                    "accession unexpectedly present",
                )
        elif d.category == "bad_timestamp":
            info = idx.parsed.get((hosp, d.control_id or ""))
            check(info is not None, d, "seeded message not found")
            if info is not None:
                obr22 = info.get("obr22", "")
                # An empty OBR-22 would be ACCEPTED by the pipeline, so it is
                # not a malformed-timestamp seed; require a present-but-bad value.
                check(bool(obr22), d, "OBR-22 empty or absent")
                if obr22:
                    try:
                        parse_hl7_ts(obr22, default_tz=registry.by_id[hosp].tzinfo)
                        check(False, d, "OBR-22 unexpectedly parseable")
                    except TimestampError:
                        pass
        elif d.category == "parse_error":
            check(
                any(h == hosp for h, _ in idx.unparseable),
                d,
                "no unparseable file in hospital stream",
            )
        elif d.category == "unknown_hospital":
            found = [
                info
                for (_h, ctrl), info in idx.parsed.items()
                if ctrl == d.control_id
                and registry.match_facility(info["msh4"], info["msh6"]) is None
            ]
            check(bool(found), d, "no message with unknown facility found")
        elif d.category == "unexpected_message_type":
            info = idx.parsed.get((hosp, d.control_id or ""))
            check(info is not None, d, "seeded message not found")
            if info is not None:
                hospital = registry.by_id[hosp]
                check(
                    info["type"] not in hospital.expected_message_types,
                    d,
                    "type unexpectedly allowed",
                )
        elif d.category in ("ack_errored", "ack_rejected"):
            codes = idx.acks_by_msa.get((hosp, d.control_id or ""), [])
            wanted = "AE" if d.category == "ack_errored" else "AR"
            check(wanted in codes, d, f"no {wanted} ACK for control id")
            check(
                (d.control_id or "") in idx.outbound_controls.get(hosp, set()),
                d,
                "control id not in outbound log",
            )
        elif d.category == "ack_missing":
            check(
                (hosp, d.control_id or "") not in idx.acks_by_msa,
                d,
                "unexpected ACK exists",
            )
            check(
                (d.control_id or "") in idx.outbound_controls.get(hosp, set()),
                d,
                "control id not in outbound log",
            )
        elif d.category == "ack_duplicate":
            codes = idx.acks_by_msa.get((hosp, d.control_id or ""), [])
            check(len(codes) >= 2, d, f"expected >=2 ACKs, found {len(codes)}")
        elif d.category == "ack_unknown_control":
            check(
                (d.control_id or "") not in idx.outbound_controls.get(hosp, set()),
                d,
                "control id unexpectedly in outbound log",
            )
            check((hosp, d.control_id or "") in idx.acks_by_msa, d, "seeded ACK not found")
        elif d.category == "unmerged_duplicate":
            patients = idx.adt_patients.get(hosp, [])
            rows = [p for p in patients if p[0] in d.mrns]
            check(len({p[0] for p in rows}) == 2, d, "need two ADT patients with seeded MRNs")
            if len(rows) >= 2:
                check(
                    len({(p[1], p[2], p[3]) for p in rows}) == 1,
                    d,
                    "duplicate pair demographics differ",
                )
            merged = {
                (s, m) for (h, s, m) in idx.merged_pairs if h == hosp and {s, m} <= set(d.mrns)
            }
            check(not merged, d, "pair was merged after all")
        else:  # pragma: no cover - CATEGORIES guard in Manifest.add
            problems.append(f"{d.id}: verifier has no check for category {d.category!r}")

    return problems
