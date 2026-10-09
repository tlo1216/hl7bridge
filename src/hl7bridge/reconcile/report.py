"""Text rendering for reconciliation reports. All rounding happens here,
at display time, never in the engine."""

from __future__ import annotations

from .engine import CATEGORIES, HospitalReconcileReport


def render_reconcile_text(reports: dict[str, HospitalReconcileReport]) -> str:
    lines: list[str] = []
    for hosp_id in sorted(reports):
        r = reports[hosp_id]
        lines.append(f"== {hosp_id} ({r.display_name}) ==")
        lines.append(
            f"  matched: {r.matched}   pending orders: {r.pending_orders}"
            f"   pending studies: {r.pending_studies}   findings: {len(r.findings)}"
        )
        for category in CATEGORIES:
            for f in r.by_category(category):
                lines.append(f"  [{f.category}] accession={f.accession or '(none)'}  {f.detail}")
        lines.append("")
    return "\n".join(lines)
