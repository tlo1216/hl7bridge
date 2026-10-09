"""Project 9: deterministic synthetic day of HL7 + DICOM traffic.

Same seed, same bytes. Every deliberate defect is labeled in the manifest,
and the generator computes the expected downstream outcomes (quarantine
counts, reconciliation flags, ACK outcomes, FHIR resource counts) as it
seeds them, so tests assert against ground truth instead of re-deriving it.

Per hospital, one simulated day (2026-10-07):
- 8 patients (A04 each; one A08 update; the last two are an unmerged
  duplicate pair; HOSPA/HOSPC also merge patient 5 into patient 4 via A40).
- 10 orders: i=0 order-without-image, i=1 cancelled, i=2 corrected report,
  i=3 study with wrong patient, i=4 study with wrong modality, rest clean.
- Completed orders get a DICOM study, optional prelim ORU, a final ORU,
  an outbound report log entry, and an inbound ACK per the defect plan
  (j=0 AE, j=1 AR, j=2 missing, j=3 duplicate AA, rest AA) plus one ACK
  for a control id we never sent.
- Message-level defects: missing accession, malformed timestamp, garbage
  bytes, an unknown facility (HOSPA stream), an unexpected SIU (HOSPB).
"""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import HospitalConfig, HospitalRegistry
from ..store import utc_iso
from .dicomgen import StudySpec, write_study
from .hl7build import message, msh, obr, seg
from .manifest import Defect, Manifest
from .patients import PatientSpec, make_pool

BASE_DATE = "2026-10-07"
REPORT_NOW_UTC = "2026-10-08T12:00:00+00:00"
N_PATIENTS = 8
N_ORDERS = 10
SKIP_ORDER_PATIENT_IDX = 5  # merged away in HOSPA/HOSPC; keep orders off it

PROCEDURES: dict[str, tuple[str, str]] = {
    "CT": ("71250", "CT CHEST WO CONTRAST"),
    "MR": ("70551", "MRI BRAIN WO CONTRAST"),
    "US": ("76700", "US ABDOMEN COMPLETE"),
    "XR": ("71045", "XRAY CHEST 1 VIEW"),
}
MODALITY_CYCLE = ("CT", "MR", "US", "XR")

REPORT_LINES = (
    "FINDINGS: Lungs are clear. No pleural effusion.",
    "FINDINGS: No focal abnormality identified.",
    "FINDINGS: Unremarkable examination for age.",
)
IMPRESSION = "IMPRESSION: No acute findings."
CORRECTED_LINE = "ADDENDUM: Corrected report; measurement value revised."


@dataclass(slots=True)
class _Event:
    at_utc: datetime
    name: str
    text: str
    expect_aa: bool


class _HospitalDay:
    def __init__(self, cfg: HospitalConfig, seed: int, manifest: Manifest) -> None:
        self.cfg = cfg
        self.seed = seed
        self.manifest = manifest
        self.tag = cfg.hospital_id[-1]
        self.rng = random.Random(f"{seed}:{cfg.hospital_id}")
        self.pool: list[PatientSpec] = make_pool(cfg.hospital_id, N_PATIENTS)
        self.events: list[_Event] = []
        self.outbound: list[dict[str, str]] = []
        self.study_specs: list[StudySpec] = []
        self.quarantine: Counter[str] = Counter()
        self.reconcile_expect: dict[str, Any] = {
            "ORDER_WITHOUT_IMAGE": [],
            "IMAGE_WITHOUT_ORDER": [],
            "MISMATCH_PATIENT": [],
            "MISMATCH_MODALITY": [],
            "matched": 0,
        }
        self.ack_expect: dict[str, int] = {}
        self.fhir_expect: dict[str, int] = {}

    # -- small helpers -------------------------------------------------------

    def local(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 10, 7, hour, minute, tzinfo=self.cfg.tzinfo)

    def ts(self, dt: datetime) -> str:
        from ..hl7time import format_hl7_ts

        return format_hl7_ts(dt, include_offset=self.cfg.timestamps_include_offset)

    def emit(self, at: datetime, name: str, text: str, *, expect_aa: bool = True) -> None:
        self.events.append(_Event(at.astimezone(UTC), name, text, expect_aa))

    def _msh(
        self,
        msg_type: str,
        control_id: str,
        at: datetime,
        *,
        facility: str | None = None,
        sending_app: str = "RIS",
    ) -> str:
        return msh(
            sending_app=sending_app,
            sending_facility=facility or self.cfg.facility_codes[0],
            receiving_app="HL7BRIDGE",
            receiving_facility="HUB",
            ts=at,
            include_offset=self.cfg.timestamps_include_offset,
            message_type=msg_type,
            control_id=control_id,
            version=self.cfg.hl7_version,
        )

    def _pid(self, p: PatientSpec) -> str:
        return seg(
            "PID",
            "1",
            "",
            f"{p.mrn}^^^{self.cfg.hospital_id}^MR",
            "",
            f"{p.family}^{p.given}",
            "",
            p.birth_date,
            p.sex,
        )

    def accession(self, i: int) -> str:
        return f"ACC{self.tag}{i:04d}"

    def _accession_fields(self, accession: str) -> tuple[str, str]:
        """(ORC-3 value, OBR-3 value) for this hospital's dialect. HOSPC
        fills both, like many real sites; the others fill only their
        configured field so config-driven extraction is actually exercised."""
        if self.cfg.hospital_id == "HOSPC":
            return accession, accession
        if self.cfg.accession_field.startswith("ORC"):
            return accession, ""
        return "", accession

    # -- message builders ----------------------------------------------------

    def build_adt(
        self,
        trigger: str,
        control_id: str,
        p: PatientSpec,
        at: datetime,
        *,
        facility: str | None = None,
        merged_mrn: str | None = None,
    ) -> str:
        segments = [
            self._msh(f"ADT^{trigger}", control_id, at, facility=facility, sending_app="ADT"),
            seg("EVN", trigger, self.ts(at)),
            self._pid(p),
        ]
        if merged_mrn is not None:
            segments.append(seg("MRG", f"{merged_mrn}^^^{self.cfg.hospital_id}^MR"))
        segments.append(seg("PV1", "1", "O"))
        return message(*segments)

    def build_orm(
        self,
        control_id: str,
        p: PatientSpec,
        accession: str,
        modality: str,
        at: datetime,
        *,
        order_control: str = "NW",
        omit_accession: bool = False,
    ) -> str:
        code, desc = PROCEDURES[modality]
        orc3, obr3 = self._accession_fields(accession)
        if omit_accession:
            orc3 = obr3 = ""
        placer = f"PL{control_id}"
        segments = [
            self._msh("ORM^O01", control_id, at),
            self._pid(p),
            seg("ORC", order_control, placer, orc3, "", "", "", "", "", self.ts(at)),
            obr(
                placer=placer,
                filler=obr3,
                procedure_code=code,
                procedure_desc=desc,
                code_system=self.cfg.procedure_code_system,
                observed=self.ts(at),
                modality=modality,
            ),
        ]
        if self.cfg.z_segment is not None:
            segments.append(self.cfg.z_segment)
        return message(*segments)

    def build_oru(
        self,
        control_id: str,
        p: PatientSpec,
        accession: str,
        modality: str,
        observed_at: datetime,
        result_at: datetime,
        status: str,
        lines: list[str],
        *,
        result_ts_override: str | None = None,
    ) -> str:
        code, desc = PROCEDURES[modality]
        orc3, obr3 = self._accession_fields(accession)
        result_ts = result_ts_override if result_ts_override is not None else self.ts(result_at)
        segments = [
            self._msh("ORU^R01", control_id, result_at),
            self._pid(p),
            seg("ORC", "RE", f"PL{control_id}", orc3),
            obr(
                placer=f"PL{control_id}",
                filler=obr3,
                procedure_code=code,
                procedure_desc=desc,
                code_system=self.cfg.procedure_code_system,
                observed=self.ts(observed_at),
                result_ts=result_ts,
                modality=modality,
                status=status,
            ),
        ]
        for n, line in enumerate(lines, start=1):
            segments.append(
                seg("OBX", str(n), "TX", "RPT^Report Text", "", line, "", "", "", "", "", status)
            )
        return message(*segments)

    def build_ack_in(
        self, control_id: str, ref_control: str, code: str, at: datetime, reason: str = ""
    ) -> str:
        segments = [
            self._msh("ACK^R01", control_id, at, sending_app="EHR"),
            seg("MSA", code, ref_control, reason) if reason else seg("MSA", code, ref_control),
        ]
        return message(*segments)

    # -- the day -------------------------------------------------------------

    def run(self) -> None:
        cfg = self.cfg
        hosp = cfg.hospital_id
        tag = self.tag

        # Registrations for the whole pool, one A08 update for patient 0.
        for i, p in enumerate(self.pool):
            at = self.local(6, 30) + timedelta(minutes=3 * i)
            self.emit(at, f"ADT{tag}{i:04d}", self.build_adt("A04", f"ADT{tag}{i:04d}", p, at))
        at = self.local(6, 58)
        self.emit(at, f"ADTU{tag}0001", self.build_adt("A08", f"ADTU{tag}0001", self.pool[0], at))

        # The last two pool entries are the unmerged duplicate pair.
        dup_a, dup_b = self.pool[N_PATIENTS - 2], self.pool[N_PATIENTS - 1]
        self.manifest.add(
            Defect(
                id=f"{hosp}-unmerged_duplicate",
                category="unmerged_duplicate",
                hospital_id=hosp,
                mrns=(dup_a.mrn, dup_b.mrn),
                note="same demographics, two MRNs, no A40",
            )
        )

        # A proper merge (patient 5 into patient 4) where A40 is expected.
        if "ADT^A40" in cfg.expected_message_types:
            at = self.local(7, 10)
            self.emit(
                at,
                f"ADTM{tag}0001",
                self.build_adt(
                    "A40", f"ADTM{tag}0001", self.pool[4], at, merged_mrn=self.pool[5].mrn
                ),
            )

        orderable = [i for i in range(N_PATIENTS) if i != SKIP_ORDER_PATIENT_IDX]
        completed: list[tuple[int, str, PatientSpec, str, datetime]] = []

        for i in range(N_ORDERS):
            p = self.pool[self.rng.choice(orderable)]
            modality = MODALITY_CYCLE[i % len(MODALITY_CYCLE)]
            accession = self.accession(i)
            order_at = self.local(7, 0) + timedelta(minutes=30 + 25 * i)
            orm_ctl = f"ORM{tag}{i:04d}"
            self.emit(order_at, orm_ctl, self.build_orm(orm_ctl, p, accession, modality, order_at))

            if i == 0:
                self.reconcile_expect["ORDER_WITHOUT_IMAGE"].append(accession)
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-order_without_image",
                        category="order_without_image",
                        hospital_id=hosp,
                        accession=accession,
                    )
                )
                continue
            if i == 1:
                cancel_at = order_at + timedelta(minutes=15)
                ctl = f"ORMC{tag}{i:04d}"
                self.emit(
                    cancel_at,
                    ctl,
                    self.build_orm(ctl, p, accession, modality, cancel_at, order_control="CA"),
                )
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-cancelled_order",
                        category="cancelled_order",
                        hospital_id=hosp,
                        accession=accession,
                        note="cancelled before imaging; must produce zero reconcile flags",
                    )
                )
                continue

            study_at = order_at + timedelta(minutes=40)
            study_patient = p
            study_modality = modality
            if i == 3:
                study_patient = next(
                    q for q in self.pool if q.mrn != p.mrn and q.mrn != self.pool[5].mrn
                )
                self.reconcile_expect["MISMATCH_PATIENT"].append(accession)
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-mismatch_patient",
                        category="mismatch_patient",
                        hospital_id=hosp,
                        accession=accession,
                        mrns=(p.mrn, study_patient.mrn),
                    )
                )
            elif i == 4:
                study_modality = "US" if modality != "US" else "XR"
                self.reconcile_expect["MISMATCH_MODALITY"].append(accession)
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-mismatch_modality",
                        category="mismatch_modality",
                        hospital_id=hosp,
                        accession=accession,
                        note=f"ordered {modality}, study says {study_modality}",
                    )
                )
            else:
                self.reconcile_expect["matched"] += 1

            self.study_specs.append(
                StudySpec(
                    hospital_id=hosp,
                    accession=accession,
                    modality=study_modality,
                    patient_mrn=study_patient.mrn,
                    patient_family=study_patient.family,
                    patient_given=study_patient.given,
                    study_at=study_at,
                    include_offset_tag=cfg.timestamps_include_offset,
                    uid_entropy=f"{self.seed}:{hosp}:{accession}",
                )
            )

            lines = [self.rng.choice(REPORT_LINES), IMPRESSION]
            if i % 2 == 0:
                prelim_at = study_at + timedelta(minutes=30)
                ctl = f"ORUP{tag}{i:04d}"
                self.emit(
                    prelim_at,
                    ctl,
                    self.build_oru(ctl, p, accession, modality, study_at, prelim_at, "P", lines),
                )
            final_at = study_at + timedelta(minutes=110)
            ctl = f"ORUF{tag}{i:04d}"
            self.emit(
                final_at,
                ctl,
                self.build_oru(ctl, p, accession, modality, study_at, final_at, "F", lines),
            )
            if i == 2:
                corrected_at = final_at + timedelta(minutes=90)
                ctl = f"ORUC{tag}{i:04d}"
                self.emit(
                    corrected_at,
                    ctl,
                    self.build_oru(
                        ctl,
                        p,
                        accession,
                        modality,
                        study_at,
                        corrected_at,
                        "C",
                        [*lines, CORRECTED_LINE],
                    ),
                )
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-corrected_report",
                        category="corrected_report",
                        hospital_id=hosp,
                        accession=accession,
                        note="C after F must update, not duplicate",
                    )
                )
            completed.append((i, accession, p, modality, final_at))

        # Orphan study: image without any order.
        orphan_acc = f"ACCX{tag}0001"
        self.study_specs.append(
            StudySpec(
                hospital_id=hosp,
                accession=orphan_acc,
                modality="CT",
                patient_mrn=self.pool[2].mrn,
                patient_family=self.pool[2].family,
                patient_given=self.pool[2].given,
                study_at=self.local(9, 45),
                include_offset_tag=cfg.timestamps_include_offset,
                uid_entropy=f"{self.seed}:{hosp}:{orphan_acc}",
            )
        )
        self.reconcile_expect["IMAGE_WITHOUT_ORDER"].append(orphan_acc)
        self.manifest.add(
            Defect(
                id=f"{hosp}-image_without_order",
                category="image_without_order",
                hospital_id=hosp,
                accession=orphan_acc,
            )
        )

        self._outbound_and_acks(completed)
        self._message_defects()

        merged_away = 1 if "ADT^A40" in cfg.expected_message_types else 0
        self.fhir_expect = {
            "patients": N_PATIENTS - merged_away,
            "service_requests": N_ORDERS,
            "diagnostic_reports": len(completed),
            "imaging_studies": len(self.study_specs),
        }

    def _outbound_and_acks(
        self, completed: list[tuple[int, str, PatientSpec, str, datetime]]
    ) -> None:
        hosp = self.cfg.hospital_id
        tag = self.tag
        outcomes: Counter[str] = Counter()
        for j, (_i, accession, _p, _modality, final_at) in enumerate(completed):
            rpt_ctl = f"RPT{tag}{j:04d}"
            sent_at = final_at + timedelta(minutes=5)
            self.outbound.append(
                {
                    "hospital_id": hosp,
                    "control_id": rpt_ctl,
                    "accession": accession,
                    "sent_at_utc": utc_iso(sent_at),
                }
            )
            ack_at = sent_at + timedelta(minutes=12)
            if j == 0:
                self.emit(
                    ack_at,
                    f"ACKH{tag}{j:04d}",
                    self.build_ack_in(
                        f"ACKH{tag}{j:04d}", rpt_ctl, "AE", ack_at, "Application error at receiver"
                    ),
                )
                outcomes["ERRORED"] += 1
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-ack_errored",
                        category="ack_errored",
                        hospital_id=hosp,
                        accession=accession,
                        control_id=rpt_ctl,
                    )
                )
            elif j == 1:
                self.emit(
                    ack_at,
                    f"ACKH{tag}{j:04d}",
                    self.build_ack_in(
                        f"ACKH{tag}{j:04d}", rpt_ctl, "AR", ack_at, "Patient not on file"
                    ),
                )
                outcomes["REJECTED"] += 1
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-ack_rejected",
                        category="ack_rejected",
                        hospital_id=hosp,
                        accession=accession,
                        control_id=rpt_ctl,
                    )
                )
            elif j == 2:
                outcomes["NO_ACK"] += 1
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-ack_missing",
                        category="ack_missing",
                        hospital_id=hosp,
                        accession=accession,
                        control_id=rpt_ctl,
                        note="no ACK ever arrives; must be flagged after timeout",
                    )
                )
            elif j == 3:
                for k in (1, 2):
                    self.emit(
                        ack_at + timedelta(minutes=k - 1),
                        f"ACKD{tag}{j:04d}{k}",
                        self.build_ack_in(f"ACKD{tag}{j:04d}{k}", rpt_ctl, "AA", ack_at),
                    )
                outcomes["DELIVERED"] += 1
                self.manifest.add(
                    Defect(
                        id=f"{hosp}-ack_duplicate",
                        category="ack_duplicate",
                        hospital_id=hosp,
                        accession=accession,
                        control_id=rpt_ctl,
                    )
                )
            else:
                self.emit(
                    ack_at,
                    f"ACKH{tag}{j:04d}",
                    self.build_ack_in(f"ACKH{tag}{j:04d}", rpt_ctl, "AA", ack_at),
                )
                outcomes["DELIVERED"] += 1

        ghost_ctl = f"RPTGHOST{tag}"
        at = self.local(16, 0)
        self.emit(
            at,
            f"ACKG{tag}0001",
            self.build_ack_in(f"ACKG{tag}0001", ghost_ctl, "AA", at),
        )
        self.manifest.add(
            Defect(
                id=f"{self.cfg.hospital_id}-ack_unknown_control",
                category="ack_unknown_control",
                hospital_id=self.cfg.hospital_id,
                control_id=ghost_ctl,
                note="ACK references a control id we never sent",
            )
        )
        self.ack_expect = {
            "DELIVERED": outcomes["DELIVERED"],
            "ERRORED": outcomes["ERRORED"],
            "REJECTED": outcomes["REJECTED"],
            "NO_ACK": outcomes["NO_ACK"],
            "duplicate_acks": 1,
            "unknown_control_acks": 1,
        }

    def _message_defects(self) -> None:
        hosp = self.cfg.hospital_id
        tag = self.tag

        at = self.local(12, 0)
        self.emit(at, f"GARBAGE{tag}", "THIS IS NOT AN HL7 MESSAGE \x00\x00", expect_aa=False)
        self.quarantine["parse_error"] += 1
        self.manifest.add(
            Defect(id=f"{hosp}-parse_error", category="parse_error", hospital_id=hosp)
        )

        if hosp == "HOSPA":
            at = self.local(12, 5)
            ctl = f"UNKH{tag}0001"
            self.emit(
                at,
                ctl,
                self.build_adt("A04", ctl, self.pool[0], at, facility="NOWHERE"),
                expect_aa=False,
            )
            self.quarantine["unknown_hospital"] += 1
            self.manifest.add(
                Defect(
                    id="GLOBAL-unknown_hospital",
                    category="unknown_hospital",
                    hospital_id=None,
                    control_id=ctl,
                    note="MSH facility NOWHERE; delivered in HOSPA stream",
                )
            )

        if hosp == "HOSPB":
            at = self.local(12, 10)
            ctl = f"SIU{tag}0001"
            segments = [
                self._msh("SIU^S12", ctl, at),
                self._pid(self.pool[1]),
            ]
            self.emit(at, ctl, message(*segments), expect_aa=False)
            self.quarantine["unexpected_message_type"] += 1
            self.manifest.add(
                Defect(
                    id=f"{hosp}-unexpected_message_type",
                    category="unexpected_message_type",
                    hospital_id=hosp,
                    control_id=ctl,
                )
            )

        at = self.local(12, 20)
        ctl = f"ORMX{tag}0001"
        self.emit(
            at,
            ctl,
            self.build_orm(ctl, self.pool[1], "IGNORED", "CT", at, omit_accession=True),
            expect_aa=False,
        )
        self.quarantine["missing_accession"] += 1
        self.manifest.add(
            Defect(
                id=f"{hosp}-missing_accession",
                category="missing_accession",
                hospital_id=hosp,
                control_id=ctl,
            )
        )

        at = self.local(12, 30)
        ctl = f"ORUX{tag}0001"
        acc = self.accession(5)
        self.emit(
            at,
            ctl,
            self.build_oru(
                ctl,
                self.pool[1],
                acc,
                "CT",
                at,
                at,
                "F",
                ["FINDINGS: duplicate transmission with corrupt clock."],
                result_ts_override="2026131599",
            ),
            expect_aa=False,
        )
        self.quarantine["bad_timestamp"] += 1
        self.manifest.add(
            Defect(
                id=f"{hosp}-bad_timestamp",
                category="bad_timestamp",
                hospital_id=hosp,
                control_id=ctl,
                note="OBR-22 is 2026131599",
            )
        )


def generate(out_dir: Path, registry: HospitalRegistry, seed: int) -> Manifest:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        seed=seed,
        base_date=BASE_DATE,
        report_now_utc=REPORT_NOW_UTC,
        hospitals=sorted(registry.by_id),
    )
    quarantine_total: Counter[str] = Counter()
    reconcile_expect: dict[str, Any] = {}
    ack_expect: dict[str, Any] = {}
    fhir_total: Counter[str] = Counter()
    aa_total = 0
    outbound_lines: list[dict[str, str]] = []

    for hosp_id in sorted(registry.by_id):
        day = _HospitalDay(registry.by_id[hosp_id], seed, manifest)
        day.run()

        msg_dir = out_dir / "messages" / hosp_id
        msg_dir.mkdir(parents=True, exist_ok=True)
        for n, event in enumerate(sorted(day.events, key=lambda e: (e.at_utc, e.name))):
            (msg_dir / f"{n:04d}_{event.name}.hl7").write_text(event.text, encoding="utf-8")
            aa_total += 1 if event.expect_aa else 0

        dicom_dir = out_dir / "dicom" / hosp_id
        for spec in day.study_specs:
            write_study(dicom_dir, spec)

        outbound_lines.extend(day.outbound)
        quarantine_total.update(day.quarantine)
        reconcile_expect[hosp_id] = day.reconcile_expect
        ack_expect[hosp_id] = day.ack_expect
        fhir_total.update(day.fhir_expect)

    outbound_path = out_dir / "outbound_reports.jsonl"
    outbound_path.write_text(
        "".join(json.dumps(line, sort_keys=True) + "\n" for line in outbound_lines),
        encoding="utf-8",
    )

    manifest.expected = {
        "quarantine": dict(sorted(quarantine_total.items())),
        "reconcile": reconcile_expect,
        "acks": ack_expect,
        "fhir": dict(sorted(fhir_total.items())),
        "messages_aa_total": aa_total,
    }
    manifest.save(out_dir / "manifest.json")
    return manifest
