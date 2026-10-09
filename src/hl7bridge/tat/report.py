"""Text rendering for TAT reports. The only place numbers are rounded."""

from __future__ import annotations

from statistics import median

from .engine import HospitalTat


def _plain_english(r: HospitalTat) -> str:
    stat_breaches = sum(1 for m in r.measured if m.breached and m.priority == "S")
    parts = [
        f"{r.hospital_id}, {r.month}: {len(r.measured)} finalized exams measured",
    ]
    if r.measured:
        values = [m.exam_to_final_hours for m in r.measured]
        parts.append(f"median exam-to-final {median(values):.2f}h")
        parts.append(f"{r.breaches} target breach(es), {stat_breaches} stat")
    missing_bits = []
    if r.missing_exam_time:
        missing_bits.append(f"{r.missing_exam_time} missing exam time (OBR-7)")
    if r.missing_final_time:
        missing_bits.append(f"{r.missing_final_time} missing final time (OBR-22)")
    if r.missing_order:
        missing_bits.append(f"{r.missing_order} with no matching order")
    if missing_bits:
        parts.append("incomplete data: " + ", ".join(missing_bits))
    if r.corrected:
        parts.append(f"{r.corrected} corrected report(s)")
    return ". ".join(parts) + "."


def render_tat_text(reports: dict[str, HospitalTat]) -> str:
    lines: list[str] = []
    for hosp_id in sorted(reports):
        r = reports[hosp_id]
        lines.append(f"== {hosp_id} ({r.display_name}) month {r.month} ==")
        lines.append(
            f"  finals: {r.finals}   measured: {len(r.measured)}"
            f"   missing exam time: {r.missing_exam_time}"
            f"   missing final time: {r.missing_final_time}"
            f"   no order: {r.missing_order}"
        )
        lines.append(
            f"  with prelim: {r.prelims}   corrected events: {r.corrected}   breaches: {r.breaches}"
        )
        for (modality, priority), g in r.groups.items():
            lines.append(
                f"  [{modality}/{priority}] n={g.n}"
                f"  median={g.median_hours:.2f}h  p90={g.p90_hours:.2f}h"
                f"  target={g.target_hours:.2f}h  breaches={g.breaches}"
            )
        lines.append(f"  {_plain_english(r)}")
        lines.append("")
    return "\n".join(lines)
