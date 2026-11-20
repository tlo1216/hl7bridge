"""``hl7bridge fhir reset --run-id X``: delete one lab run, nothing else.

This is the only command in hl7bridge that deletes from a FHIR server, so
it is narrow on purpose:

- **One run only.** It finds resources by the run tag
  (``<lab tag system>/lab-run``, code = run id) and nothing else. There is
  no "all runs" scope and no flag that widens it. Without a run id it
  refuses to start.
- **Never a Patient.** It searches Observation, DiagnosticReport,
  ServiceRequest and Encounter only, and a found resource that is not one
  of those, or lacks the run tag, stops the command before anything is
  deleted.
- **Dry run by default.** It prints the counts per type and deletes
  nothing unless ``--yes`` is given and the run id is typed back exactly.
- **All or nothing.** The deletes go in one transaction (results first,
  then reports, orders, visits). If the server refuses any of them (a
  resource outside the run still refers to one, say), nothing is deleted
  and the command exits non-zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import BridgeError
from .client import HapiClient
from .runscope import RUN_TYPES, RunScope


class ResetRefused(BridgeError):
    """A found resource is not one reset may delete."""


@dataclass(slots=True)
class ResetPlan:
    scope: RunScope
    ids: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.ids.values())


def plan_reset(client: HapiClient, scope: RunScope) -> ResetPlan:
    """Every resource of the run, by type, read across all result pages."""
    system, code = scope.run_tag
    plan = ResetPlan(scope)
    for kind in RUN_TYPES:
        found = client.search_all(kind, {"_tag": f"{system}|{code}", "_count": "100"})
        ids: list[str] = []
        for resource in found:
            if resource.get("resourceType") != kind or not scope.owns(resource):
                raise ResetRefused(
                    f"the server returned a {kind} search result without this run's tag;"
                    " nothing was deleted."
                )
            ids.append(str(resource["id"]))
        plan.ids[kind] = sorted(set(ids))
    return plan


def delete_bundle(plan: ResetPlan) -> dict[str, Any]:
    entries = [
        {"request": {"method": "DELETE", "url": f"{kind}/{server_id}"}}
        for kind in RUN_TYPES
        for server_id in plan.ids.get(kind, [])
    ]
    for entry in entries:
        if entry["request"]["url"].split("/", 1)[0] not in RUN_TYPES:  # pragma: no cover
            raise ResetRefused("a delete outside the run types was planned; nothing was deleted.")
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def execute_reset(client: HapiClient, plan: ResetPlan) -> int:
    """Delete the plan in one transaction. Returns how many were deleted."""
    if plan.total == 0:
        return 0
    client.commit(delete_bundle(plan), ok_codes=(200, 202, 204))
    return plan.total


__all__ = ["ResetPlan", "ResetRefused", "delete_bundle", "execute_reset", "plan_reset"]
