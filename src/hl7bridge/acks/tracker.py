"""Report delivery tracking: every outbound ORU ends in exactly one state.

States:
- DELIVERED: first ACK received was AA.
- ERRORED:   first ACK received was AE.
- REJECTED:  first ACK received was AR.
- NO_ACK:    nothing received and the hospital's timeout has elapsed.
- PENDING:   nothing received, still inside the timeout window.

The first ACK (by received time) decides the terminal state; later ACKs are
counted as duplicates, and a later ACK that disagrees with the first is
reported as a conflict, never silently reconciled. ACKs whose control id
matches nothing we sent are listed separately, never dropped. Outbound
reports whose hospital is no longer configured cannot be judged against a
timeout; they are counted in their own loud bucket, never dropped, and never
named (the control id is MSH-10, which can hold anything).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import HospitalRegistry
from ..store.db import parse_utc_iso

STATES = ("DELIVERED", "ERRORED", "REJECTED", "NO_ACK", "PENDING")
_CODE_TO_STATE = {"AA": "DELIVERED", "AE": "ERRORED", "AR": "REJECTED"}


@dataclass(frozen=True, slots=True)
class OutboundOutcome:
    hospital_id: str
    control_id: str
    accession: str
    state: str
    sent_at_utc: str
    reason: str | None = None
    acked_at_utc: str | None = None
    duplicate_acks: int = 0
    conflicting_codes: tuple[str, ...] = ()


@dataclass(slots=True)
class AckReport:
    outcomes: list[OutboundOutcome] = field(default_factory=list)
    unknown_acks: list[sqlite3.Row] = field(default_factory=list)
    unconfigured: list[sqlite3.Row] = field(default_factory=list)

    def by_state(self, hospital_id: str | None = None) -> dict[str, list[OutboundOutcome]]:
        result: dict[str, list[OutboundOutcome]] = {state: [] for state in STATES}
        for outcome in self.outcomes:
            if hospital_id is None or outcome.hospital_id == hospital_id:
                result[outcome.state].append(outcome)
        return result


def track_acks(conn: sqlite3.Connection, registry: HospitalRegistry, now: datetime) -> AckReport:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("track_acks() requires an aware 'now'")

    report = AckReport()
    acks_by_key: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM acks_in ORDER BY received_at_utc, id").fetchall():
        acks_by_key.setdefault((row["hospital_id"], row["msa_control_id"]), []).append(row)

    outbound = conn.execute(
        "SELECT * FROM outbound_reports ORDER BY hospital_id, control_id"
    ).fetchall()
    outbound_keys = {(row["hospital_id"], row["control_id"]) for row in outbound}

    for row in outbound:
        hospital = registry.by_id.get(row["hospital_id"])
        if hospital is None:
            report.unconfigured.append(row)
            continue
        key = (row["hospital_id"], row["control_id"])
        acks = acks_by_key.get(key, [])
        if not acks:
            timeout = timedelta(minutes=hospital.ack_timeout_minutes)
            state = "NO_ACK" if now - parse_utc_iso(row["sent_at_utc"]) > timeout else "PENDING"
            report.outcomes.append(
                OutboundOutcome(
                    hospital_id=row["hospital_id"],
                    control_id=row["control_id"],
                    accession=row["accession"],
                    state=state,
                    sent_at_utc=row["sent_at_utc"],
                )
            )
            continue
        first = acks[0]
        conflicts = tuple(
            sorted({a["msa_code"] for a in acks[1:] if a["msa_code"] != first["msa_code"]})
        )
        report.outcomes.append(
            OutboundOutcome(
                hospital_id=row["hospital_id"],
                control_id=row["control_id"],
                accession=row["accession"],
                state=_CODE_TO_STATE[first["msa_code"]],
                sent_at_utc=row["sent_at_utc"],
                reason=first["reason"],
                acked_at_utc=first["received_at_utc"],
                duplicate_acks=len(acks) - 1,
                conflicting_codes=conflicts,
            )
        )

    for key, acks in sorted(acks_by_key.items()):
        if key not in outbound_keys:
            report.unknown_acks.extend(acks)

    assert len(report.outcomes) + len(report.unconfigured) == len(outbound), (
        "terminal-state partition broken"
    )
    return report


def render_ack_text(report: AckReport, registry: HospitalRegistry) -> str:
    lines: list[str] = []
    if report.unconfigured:
        lines.append(
            f"UNCONFIGURED HOSPITAL: {len(report.unconfigured)} outbound report(s) name a"
            " hospital that is not in the config folder, so no state can be given."
            " Restore the profile or reload the outbound log."
        )
        lines.append("")
    for hosp_id in sorted(registry.by_id):
        states = report.by_state(hosp_id)
        lines.append(f"== {hosp_id} ==")
        lines.append("  " + "   ".join(f"{state}: {len(states[state])}" for state in STATES))
        for state in ("ERRORED", "REJECTED", "NO_ACK", "PENDING"):
            for o in states[state]:
                reason = f"  reason: {o.reason}" if o.reason else ""
                lines.append(
                    f"  [{state}] report={o.control_id} accession={o.accession}"
                    f" sent={o.sent_at_utc}{reason}"
                )
        for o in states["DELIVERED"]:
            notes = []
            if o.duplicate_acks:
                notes.append(f"duplicate_acks={o.duplicate_acks}")
            if o.conflicting_codes:
                notes.append(f"CONFLICTING_LATER_CODES={','.join(o.conflicting_codes)}")
            if notes:
                lines.append(f"  [DELIVERED] report={o.control_id} " + " ".join(notes))
        lines.append("")
    if report.unknown_acks:
        lines.append(f"ACKs for unknown control ids: {len(report.unknown_acks)}")
        for a in report.unknown_acks:
            lines.append(
                f"  hospital={a['hospital_id']} msa_control_id={a['msa_control_id']}"
                f" code={a['msa_code']} received={a['received_at_utc']}"
            )
    return "\n".join(lines)
