"""Turnaround-time computation.

Interval definitions (all arithmetic on aware datetimes, full precision;
rounding happens only in the renderer):

- exam time      = OBR-7 of the FIRST final (F) report event per accession
- final time     = OBR-22 of that first F event (first by result time,
                   ties broken by event insertion order)
- prelim time    = OBR-22 of the first preliminary (P) event, if any
- order time     = orders.ordered_at_utc for the same accession
- exam_to_final  = final time - exam time      (the headline metric)
- exam_to_prelim = prelim time - exam time
- order_to_exam  = exam time - order time

Corrected (C) events never move the final time: the clock stops at the
first F. C events are counted and reported.

Missing data is never dropped silently: an accession with an F event but
no usable exam or final time lands in the missing counters, is excluded
from the statistics, and is excluded from breach counting (a breach claim
needs a measured interval).

Breaches: exam_to_final strictly greater than the configured target for
the order's priority (per-hospital config; strict comparison, on-target
is not a breach).

Statistics: median via statistics.median; P90 via
statistics.quantiles(n=10, method="inclusive") (linear interpolation on
sorted data, the same convention as numpy's default percentile). For a
single data point both equal that point.

Month selection: an accession belongs to the report month if its final
time falls inside that calendar month in the HOSPITAL'S timezone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from statistics import StatisticsError, median, quantiles

from ..config import HospitalConfig, HospitalRegistry
from ..store.db import parse_utc_iso

UNSPECIFIED_PRIORITY = "unspecified"


@dataclass(frozen=True, slots=True)
class MeasuredExam:
    accession: str
    modality: str
    priority: str
    exam_to_final_hours: float
    exam_to_prelim_hours: float | None
    order_to_exam_hours: float | None
    target_hours: float

    @property
    def breached(self) -> bool:
        return self.exam_to_final_hours > self.target_hours


@dataclass(frozen=True, slots=True)
class GroupStats:
    n: int
    median_hours: float
    p90_hours: float
    breaches: int
    target_hours: float


@dataclass(slots=True)
class HospitalTat:
    hospital_id: str
    display_name: str
    month: str
    finals: int = 0  # accessions with at least one F event in the month
    measured: list[MeasuredExam] = field(default_factory=list)
    missing_exam_time: int = 0
    missing_final_time: int = 0
    missing_order: int = 0
    prelims: int = 0
    corrected: int = 0
    groups: dict[tuple[str, str], GroupStats] = field(default_factory=dict)

    @property
    def breaches(self) -> int:
        return sum(1 for m in self.measured if m.breached)


def _stats(values: list[float]) -> tuple[float, float]:
    """(median, p90) at full precision. Caller guarantees non-empty."""
    med = median(values)
    try:
        p90 = quantiles(values, n=10, method="inclusive")[-1]
    except StatisticsError:  # single data point
        p90 = values[0]
    return float(med), float(p90)


def _hours(later_iso: str, earlier_iso: str) -> float:
    return (parse_utc_iso(later_iso) - parse_utc_iso(earlier_iso)).total_seconds() / 3600.0


def _tat_hospital(conn: sqlite3.Connection, cfg: HospitalConfig, month: str) -> HospitalTat:
    result = HospitalTat(cfg.hospital_id, cfg.display_name, month)
    tz = cfg.tzinfo

    events = conn.execute(
        "SELECT * FROM report_events WHERE hospital_id = ? ORDER BY result_at_utc, id",
        (cfg.hospital_id,),
    ).fetchall()
    orders = {
        row["accession"]: row
        for row in conn.execute(
            "SELECT * FROM orders WHERE hospital_id = ?", (cfg.hospital_id,)
        ).fetchall()
    }

    by_accession: dict[str, list[sqlite3.Row]] = {}
    for e in events:
        by_accession.setdefault(e["accession"], []).append(e)

    for accession in sorted(by_accession):
        acc_events = by_accession[accession]
        finals = [e for e in acc_events if e["status"] == "F"]
        prelims = [e for e in acc_events if e["status"] == "P"]
        corrected = [e for e in acc_events if e["status"] == "C"]
        if not finals:
            continue
        first_final = finals[0]  # ordered by result_at_utc, id in the query

        final_iso = first_final["result_at_utc"]
        if not final_iso:
            # Cannot even place it in a month; counted, excluded.
            result.finals += 1
            result.missing_final_time += 1
            continue
        if parse_utc_iso(final_iso).astimezone(tz).strftime("%Y-%m") != month:
            continue
        result.finals += 1
        result.prelims += 1 if prelims else 0
        result.corrected += len(corrected)

        exam_iso = first_final["observed_at_utc"]
        if not exam_iso:
            result.missing_exam_time += 1
            continue

        order = orders.get(accession)
        order_to_exam: float | None = None
        if order is None:
            result.missing_order += 1
        else:
            order_to_exam = _hours(exam_iso, order["ordered_at_utc"])

        prelim_hours: float | None = None
        first_prelim_iso = prelims[0]["result_at_utc"] if prelims else None
        if first_prelim_iso:
            prelim_hours = _hours(first_prelim_iso, exam_iso)

        modality = (order["modality"] if order else None) or "unknown"
        priority = (order["priority"] if order else None) or UNSPECIFIED_PRIORITY
        target = cfg.tat.target_hours(priority if priority != UNSPECIFIED_PRIORITY else None)
        result.measured.append(
            MeasuredExam(
                accession=accession,
                modality=modality,
                priority=priority,
                exam_to_final_hours=_hours(final_iso, exam_iso),
                exam_to_prelim_hours=prelim_hours,
                order_to_exam_hours=order_to_exam,
                target_hours=target,
            )
        )

    grouped: dict[tuple[str, str], list[MeasuredExam]] = {}
    for m in result.measured:
        grouped.setdefault((m.modality, m.priority), []).append(m)
    for key in sorted(grouped):
        exams = grouped[key]
        values = [m.exam_to_final_hours for m in exams]
        med, p90 = _stats(values)
        result.groups[key] = GroupStats(
            n=len(exams),
            median_hours=med,
            p90_hours=p90,
            breaches=sum(1 for m in exams if m.breached),
            target_hours=exams[0].target_hours,
        )
    return result


def tat_report(
    conn: sqlite3.Connection, registry: HospitalRegistry, month: str
) -> dict[str, HospitalTat]:
    """Monthly turnaround report. ``month`` is 'YYYY-MM' in hospital local time."""
    if len(month) != 7 or month[4] != "-":
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
    return {
        hosp_id: _tat_hospital(conn, registry.by_id[hosp_id], month)
        for hosp_id in sorted(registry.by_id)
    }
