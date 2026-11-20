"""Post mapped messages to a FHIR server. ``to-fhir --post`` and ``fhir
sync`` both write through here, so they write the same content.

Each message is one transaction Bundle from ``hl7bridge.v2fhir``. Before it
is sent, two things are read from the server and folded into it:

1. **Merge** (entries with the ``merge`` policy: Patient from ADT,
   Encounter, ServiceRequest). In HL7 v2 an empty field means "no
   change", so a thin A08 must not erase the address an A01 wrote. The
   current resource is read by its identifier and the message's elements
   are laid over it: each element the mapper owns replaces the current one
   when the message has it and is kept when it does not. The write goes
   to ``<Type>/<id>`` with ``If-Match`` on the version read, so a change
   made between the read and the write fails the transaction instead of
   being lost. No current resource: the conditional update creates it.
   More than one: refused, never "first match wins".
2. **Retire** (ORU). A corrected report can drop a result. The current
   DiagnosticReport's ``result`` list is read, and each Observation in it
   whose key parses to this report's accession and that the new version no
   longer carries is written back with status ``entered-in-error`` (with
   ``If-Match``) in the same transaction. Observations with another
   system or another report's key are never touched.

``check_run_order`` refuses a run that would post an older version of a
report after a newer one (by OBR-22, then by status).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..errors import BridgeError, FhirPostError
from ..v2fhir.bundle import MERGE, MappedMessage, bundle_json, full_url, search_query
from ..v2fhir.observation import KEY_JOIN, SET_ID_PART, SUB_ID_PART
from .client import EntryResult, HapiClient

# The elements each mapper writes and so owns in a merge. Everything else
# on the current resource (meta, tags, other identifiers, extensions) stays.
OWNED: dict[str, tuple[str, ...]] = {
    "Patient": ("name", "gender", "birthDate", "address", "telecom"),
    "Encounter": ("status", "class", "subject", "period"),
    "ServiceRequest": ("status", "intent", "code", "authoredOn", "subject"),
}
# Elements merged one level down: an A08 with PV1-45 and no PV1-44 keeps
# the admit time.
NESTED = {("Encounter", "period")}
RETIRED = "entered-in-error"
# Report status order for check_run_order: a later message in a run may
# not go back down this list for the same report.
STATUS_RANK = {
    "registered": 0,
    "partial": 1,
    "preliminary": 1,
    "final": 2,
    "amended": 3,
    "corrected": 3,
    "appended": 3,
    "cancelled": 3,
    "entered-in-error": 3,
}
_REFERENCE = re.compile(r"(?:^|/)Observation/([A-Za-z0-9.\-]{1,64})(?:/_history/[^/]+)?$")


class PostOrderError(BridgeError):
    """A run would post an older version of a report after a newer one."""


@dataclass(frozen=True, slots=True)
class Committed:
    """One committed entry: resource type, key (``system|value``), the
    entry status (200 or 201), the server id when the answer gave one, and
    whether it was an Observation retired by a correction."""

    kind: str
    key: str
    code: int
    server_id: str | None
    retired: bool = False


def _key(resource: dict[str, Any]) -> str:
    ident = resource["identifier"][0]
    return f"{ident['system']}|{ident['value']}"


def merge_resource(current: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """``new`` laid over ``current``: owned elements the message has replace
    the current ones, absent ones are kept, identifiers are added to."""
    kind = new["resourceType"]
    merged = copy.deepcopy(current)
    for element in OWNED[kind]:
        if element not in new:
            continue
        if (kind, element) in NESTED and isinstance(merged.get(element), dict):
            merged[element] = {**merged[element], **new[element]}
        else:
            merged[element] = copy.deepcopy(new[element])
    have = {(i.get("system"), i.get("value")) for i in merged.get("identifier", [])}
    for ident in new.get("identifier", []):
        if (ident.get("system"), ident.get("value")) not in have:
            merged.setdefault("identifier", []).append(copy.deepcopy(ident))
    if kind == "Encounter" and merged.get("period", {}).get("end"):
        # The IG sets finished when PV1-45 is valued; a discharge already
        # on the server stays one when a later A08 leaves PV1-45 empty.
        merged["status"] = "finished"
    return merged


def _version_tag(resource: dict[str, Any], kind: str) -> str:
    version = resource.get("meta", {}).get("versionId")
    if not isinstance(version, str) or not version or '"' in version:
        raise FhirPostError(
            0, kind, reason=f"the server gave no version for the {kind}; cannot update it safely"
        )
    return f'W/"{version}"'


def _one(client: HapiClient, kind: str, key: str) -> dict[str, Any] | None:
    system, _, value = key.partition("|")
    found = client.search_identifier(kind, system, value)
    if len(found) > 1:
        raise FhirPostError(
            0, kind, reason=f"more than one {kind} has this identifier; refusing to pick one"
        )
    return found[0] if found else None


def _merge_entry(client: HapiClient, entry: dict[str, Any]) -> None:
    new = entry["resource"]
    kind = new["resourceType"]
    current = _one(client, kind, _key(new))
    if current is None:
        return  # the conditional update creates it
    merged = merge_resource(current, new)
    server_id = current["id"]
    entry["resource"] = merged
    entry["request"] = {
        "method": "PUT",
        "url": f"{kind}/{server_id}",
        "ifMatch": _version_tag(current, kind),
    }


def result_accession(value: str) -> str | None:
    """The accession part of a result key (``<accession>/<code>/sub|set/<id>``),
    or None when the value does not have exactly that shape."""
    parts = value.split(KEY_JOIN)
    if len(parts) != 4 or parts[2] not in (SUB_ID_PART, SET_ID_PART) or not all(parts):
        return None
    return parts[0]


def _retire_entries(
    client: HapiClient, mapped: MappedMessage, report: dict[str, Any], keep: set[str]
) -> list[dict[str, Any]]:
    current = _one(client, "DiagnosticReport", _key(report))
    if current is None:
        return []
    retire: list[dict[str, Any]] = []
    for ref in current.get("result", []):
        match = _REFERENCE.search(str(ref.get("reference", "")))
        if match is None:
            continue
        obs = client.read_by_id("Observation", match.group(1))
        if obs is None or obs.get("status") == RETIRED:
            continue
        ours = [
            i
            for i in obs.get("identifier", [])
            if i.get("system") == mapped.observation_system
            and result_accession(str(i.get("value", ""))) == mapped.accession
        ]
        if not ours or any(f"{i['system']}|{i['value']}" in keep for i in ours):
            continue
        obs = copy.deepcopy(obs)
        obs["status"] = RETIRED
        ident = ours[0]
        retire.append(
            {
                "fullUrl": full_url("Observation", ident["system"], ident["value"]),
                "resource": obs,
                "request": {
                    "method": "PUT",
                    "url": f"Observation/{obs['id']}",
                    "ifMatch": _version_tag(obs, "Observation"),
                },
            }
        )
    return retire


def prepare(client: HapiClient, mapped: MappedMessage) -> dict[str, Any]:
    """The transaction to send for ``mapped``: merges and retirements done."""
    body = bundle_json(mapped)
    entries: list[dict[str, Any]] = body["entry"]
    for entry in entries:
        if mapped.policies.get(entry["fullUrl"]) == MERGE:
            _merge_entry(client, entry)
    if mapped.message_type == "ORU^R01":
        report = next(
            e["resource"] for e in entries if e["resource"]["resourceType"] == "DiagnosticReport"
        )
        keep = {
            _key(e["resource"]) for e in entries if e["resource"]["resourceType"] == "Observation"
        }
        entries.extend(_retire_entries(client, mapped, report, keep))
    return body


def post_mapped(client: HapiClient, mapped: MappedMessage) -> list[Committed]:
    """Prepare and post one message. Raises FhirPostError unless the server
    committed every entry (``HapiClient.commit``)."""
    body = prepare(client, mapped)
    results: list[EntryResult] = client.commit(body)
    mapped_count = len(mapped.bundle.entry or [])
    return [
        Committed(r.kind, _key(entry["resource"]), r.code, r.server_id, retired=n >= mapped_count)
        for n, (r, entry) in enumerate(zip(results, body["entry"], strict=True))
    ]


@dataclass(frozen=True, slots=True)
class _Version:
    label: str
    rank: int
    issued: datetime | None


def check_run_order(items: Sequence[tuple[str, MappedMessage]]) -> None:
    """Refuse a run in which a message would post an older version of a
    report than one posted before it in the same run: an earlier OBR-22
    (issued), or with the same or no OBR-22 a lower status (a preliminary
    after a final, a final after a correction). Labels name the inputs;
    no value is quoted."""
    latest: dict[str, _Version] = {}
    for label, mapped in items:
        if mapped.message_type != "ORU^R01":
            continue
        report = next(
            e.resource
            for e in mapped.bundle.entry or []
            if e.resource is not None and e.resource.get_resource_type() == "DiagnosticReport"
        )
        ident = report.identifier[0]  # type: ignore[attr-defined]
        key = f"{ident.system}|{ident.value}"
        issued = report.issued  # type: ignore[attr-defined]
        version = _Version(label, STATUS_RANK.get(str(report.status), 0), issued)  # type: ignore[attr-defined]
        before = latest.get(key)
        if before is not None:
            if before.issued is not None and issued is not None and issued != before.issued:
                older = issued < before.issued
            else:
                older = version.rank < before.rank
            if older:
                raise PostOrderError(
                    f"{label} holds an older version of the same report as {before.label}"
                    " (OBR-22, then OBR-25); posting it after would undo the newer one.",
                    hint="Post the messages in the order they were sent,"
                    " or leave the older one out.",
                )
        latest[key] = version


__all__ = [
    "OWNED",
    "Committed",
    "PostOrderError",
    "check_run_order",
    "merge_resource",
    "post_mapped",
    "prepare",
    "result_accession",
    "search_query",
]
