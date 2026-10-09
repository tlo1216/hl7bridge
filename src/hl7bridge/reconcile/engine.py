"""Order-to-image reconciliation.

Matching normalizes whitespace and case ONLY (spec rule: anything fuzzier is
flagged for human review, never auto-matched). Thresholds and the grace
period for late-arriving orders come from per-hospital config.

Categories:
- ORDER_WITHOUT_IMAGE: active order, no study, older than the modality threshold.
- IMAGE_WITHOUT_ORDER: study with no matching order (or no accession at all),
  older than the order-lag grace period.
- MISMATCH_PATIENT / MISMATCH_MODALITY: same accession, conflicting data.
- CANCELLED_ORDER_WITH_IMAGE: a study arrived for a cancelled order.
A cancelled order without a study is correct behavior and is not flagged.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import HospitalConfig, HospitalRegistry
from ..store.db import parse_utc_iso

CATEGORIES = (
    "ORDER_WITHOUT_IMAGE",
    "IMAGE_WITHOUT_ORDER",
    "MISMATCH_PATIENT",
    "MISMATCH_MODALITY",
    "CANCELLED_ORDER_WITH_IMAGE",
)


@dataclass(frozen=True, slots=True)
class Finding:
    category: str
    hospital_id: str
    accession: str | None
    detail: str
    study_uid: str | None = None


@dataclass(slots=True)
class HospitalReconcileReport:
    hospital_id: str
    display_name: str
    matched: int = 0
    pending_orders: int = 0
    pending_studies: int = 0
    findings: list[Finding] = field(default_factory=list)

    def by_category(self, category: str) -> list[Finding]:
        return [f for f in self.findings if f.category == category]


def _norm(accession: str) -> str:
    return accession.strip().casefold()


def _age_hours(now: datetime, then_iso: str) -> float:
    return (now - parse_utc_iso(then_iso)).total_seconds() / 3600.0


def _reconcile_hospital(
    conn: sqlite3.Connection, cfg: HospitalConfig, now: datetime
) -> HospitalReconcileReport:
    report = HospitalReconcileReport(cfg.hospital_id, cfg.display_name)

    orders = conn.execute(
        "SELECT * FROM orders WHERE hospital_id = ? ORDER BY accession", (cfg.hospital_id,)
    ).fetchall()
    studies = conn.execute(
        "SELECT * FROM studies WHERE hospital_id = ? ORDER BY study_uid", (cfg.hospital_id,)
    ).fetchall()

    studies_by_acc: dict[str, list[sqlite3.Row]] = {}
    no_accession_studies: list[sqlite3.Row] = []
    for s in studies:
        if s["accession"] is None or not s["accession"].strip():
            no_accession_studies.append(s)
        else:
            studies_by_acc.setdefault(_norm(s["accession"]), []).append(s)

    order_accs = {_norm(o["accession"]) for o in orders}

    for o in orders:
        acc_norm = _norm(o["accession"])
        matches = studies_by_acc.get(acc_norm, [])
        if o["status"] == "cancelled":
            for s in matches:
                report.findings.append(
                    Finding(
                        "CANCELLED_ORDER_WITH_IMAGE",
                        cfg.hospital_id,
                        o["accession"],
                        f"study {s['study_uid']} exists for cancelled order",
                        study_uid=s["study_uid"],
                    )
                )
            continue
        if matches:
            clean = True
            for s in matches:
                if s["patient_mrn"] and o["patient_mrn"] and s["patient_mrn"] != o["patient_mrn"]:
                    clean = False
                    report.findings.append(
                        Finding(
                            "MISMATCH_PATIENT",
                            cfg.hospital_id,
                            o["accession"],
                            f"order patient {o['patient_mrn']} != study patient "
                            f"{s['patient_mrn']} (study {s['study_uid']})",
                            study_uid=s["study_uid"],
                        )
                    )
                if s["modality"] and o["modality"] and s["modality"] != o["modality"]:
                    clean = False
                    report.findings.append(
                        Finding(
                            "MISMATCH_MODALITY",
                            cfg.hospital_id,
                            o["accession"],
                            f"order modality {o['modality']} != study modality "
                            f"{s['modality']} (study {s['study_uid']})",
                            study_uid=s["study_uid"],
                        )
                    )
            if clean:
                report.matched += 1
            continue
        # No studies for an active order: threshold by modality.
        threshold = cfg.reconcile.threshold_hours(o["modality"] or "")
        age = _age_hours(now, o["ordered_at_utc"])
        if age > threshold:
            report.findings.append(
                Finding(
                    "ORDER_WITHOUT_IMAGE",
                    cfg.hospital_id,
                    o["accession"],
                    f"no study after {age:.1f}h (threshold {threshold:.1f}h,"
                    f" modality {o['modality'] or 'unknown'})",
                )
            )
        else:
            report.pending_orders += 1

    grace = timedelta(minutes=cfg.reconcile.order_lag_minutes)
    for acc_norm, group in sorted(studies_by_acc.items()):
        if acc_norm in order_accs:
            continue
        for s in group:
            if now - parse_utc_iso(s["study_at_utc"]) > grace:
                report.findings.append(
                    Finding(
                        "IMAGE_WITHOUT_ORDER",
                        cfg.hospital_id,
                        s["accession"],
                        f"study {s['study_uid']} has no matching order",
                        study_uid=s["study_uid"],
                    )
                )
            else:
                report.pending_studies += 1
    for s in no_accession_studies:
        if now - parse_utc_iso(s["study_at_utc"]) > grace:
            report.findings.append(
                Finding(
                    "IMAGE_WITHOUT_ORDER",
                    cfg.hospital_id,
                    None,
                    f"study {s['study_uid']} has empty AccessionNumber",
                    study_uid=s["study_uid"],
                )
            )
        else:
            report.pending_studies += 1

    return report


def reconcile(
    conn: sqlite3.Connection, registry: HospitalRegistry, now: datetime
) -> dict[str, HospitalReconcileReport]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("reconcile() requires an aware 'now'")
    return {
        hosp_id: _reconcile_hospital(conn, registry.by_id[hosp_id], now)
        for hosp_id in sorted(registry.by_id)
    }
