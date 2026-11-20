"""Patient matching by MRN for the direct-to-FHIR listener (``match_only``).

The listener never creates or changes a Patient in this mode. It searches
``Patient?identifier=<system>|<mrn>`` and acts on how many there are:

| Hits | Outcome | ACK |
| --- | --- | --- |
| none | ``unknown`` | AE ``unknown_patient`` |
| one, active, not replaced | ``one`` with its id | the message goes on |
| one, ``active`` false | ``inactive`` | AE ``inactive_patient`` |
| one, with a ``replaced-by`` link | ``replaced`` | AE ``replaced_patient`` |
| more than one | ``ambiguous`` | AE ``ambiguous_patient`` |

The count is over the whole result, never the first page: the search asks
for ``_count=2`` and ``_total=accurate``, and the larger of ``Bundle.total``
and the entries returned decides, with a ``next`` link counting as more
than one. "Pick the first" is never an outcome. Matching is the classic
patient safety risk in interfaces: a wrong match files results on the
wrong patient, so many is an error a person resolves, not a choice the
listener makes.

An inactive Patient or one replaced by another (a merge) is refused too:
results filed there would be invisible on the surviving record. The
sender fixes PID-3 and resends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..errors import FhirPostError, MappingError
from ..v2fhir.common import check_key
from .client import HapiClient

Outcome = Literal["one", "unknown", "ambiguous", "inactive", "replaced"]


@dataclass(frozen=True, slots=True)
class PatientMatch:
    outcome: Outcome
    patient_id: str | None = None


def _count(body: dict[str, Any], found: list[dict[str, Any]]) -> int:
    total = body.get("total")
    paged = any(
        isinstance(link, dict) and link.get("relation") == "next" for link in body.get("link") or []
    )
    counted = max(len(found), total if isinstance(total, int) and total >= 0 else 0)
    return max(counted, 2) if paged else counted


def resolve_patient(client: HapiClient, system: str, mrn: str) -> PatientMatch:
    """The Patient with exactly this MRN, by the table in the module text."""
    try:
        check_key(mrn, "PID-3")
    except MappingError as exc:
        raise FhirPostError(0, "Patient", reason=str(exc)) from None
    body = client.search_bundle(
        "Patient", {"identifier": f"{system}|{mrn}", "_count": "2", "_total": "accurate"}
    )
    found = [
        e["resource"]
        for e in body.get("entry") or []
        if isinstance(e, dict)
        and isinstance(e.get("resource"), dict)
        and e["resource"].get("resourceType") == "Patient"
    ]
    count = _count(body, found)
    if count == 0:
        return PatientMatch("unknown")
    if count > 1:
        return PatientMatch("ambiguous")
    if not found:
        raise FhirPostError(
            200, "Patient", reason="the search answer counts a match it does not hold"
        )
    patient = found[0]
    if patient.get("active") is False:
        return PatientMatch("inactive")
    links = patient.get("link") or []
    if any(isinstance(link, dict) and link.get("type") == "replaced-by" for link in links):
        return PatientMatch("replaced")
    server_id = patient.get("id")
    if not isinstance(server_id, str) or not server_id:
        raise FhirPostError(200, "Patient", reason="the matched Patient has no id")
    return PatientMatch("one", server_id)


__all__ = ["PatientMatch", "resolve_patient"]
