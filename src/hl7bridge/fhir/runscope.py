"""Run ownership for direct-to-FHIR writes (``listen --target fhir``).

A teaching lab has many runs (students, sessions) writing to one server.
Every write belongs to exactly one run, so no run can change or delete
another run's data:

1. **Run-namespaced identifiers.** Every identifier the listener writes
   has the system ``<profile system>/run/<run id>``, so a conditional write
   can only ever match a resource of the same run. The value from the
   message is kept beside it under the profile system with ``use:
   secondary``, for reading; it is never a match key.
2. **A run tag on every write.** Each resource carries the lab tag (the
   profile's ``fhir.tag``) and the run tag (``<lab tag system>/lab-run``,
   code = run id). A create is a conditional create on the run identifier
   *and* the run tag.
3. **Outside the run is refused.** Before a write, each key is searched.
   A match that lacks this run's tag raises OutsideRunError (AE
   ``outside_run``) and nothing is written.
4. **The Patient is read only.** It is matched by MRN
   (``fhir/match.py``) and referred to by id. It is never created, merged
   or tagged, and ``fhir reset`` never touches it.

Every existing resource is written by id with ``If-Match`` on the version
read, and every new one by a conditional create whose answer must be 201
(``post.check_created``). With the listener's per-key lock, that closes
the gap between search and write that a plain conditional update leaves.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

from ..errors import BridgeError, FhirPostError, UsageError
from ..v2fhir.bundle import MERGE, MappedMessage, bundle_json, search_query
from .client import EntryResult, HapiClient
from .post import (
    _version_tag,
    check_created,
    check_not_stale,
    merge_resource,
    retire_entries,
)

RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")
DEFAULT_LAB_TAG = ("urn:hl7bridge:tag", "hl7v2-lab")
RUN_TAG_SUFFIX = "/lab-run"
# Types the listener writes, in the order fhir reset deletes them.
RUN_TYPES = ("Observation", "DiagnosticReport", "ServiceRequest", "Encounter")


def check_run_id(value: str | None, source: str = "--run-id") -> str:
    """``value`` if it is a short safe token, else UsageError. The value is
    not echoed: it came from the command line or the environment."""
    if not value or not RUN_ID.fullmatch(value):
        raise UsageError(
            f"{source} must be 1 to 32 letters, digits or '-', starting with a letter"
            " or digit (value not shown).",
            hint="Try --run-id lab-2026-11-20-a",
        )
    return value


class OutsideRunError(BridgeError):
    """A key matched a resource that does not carry this run's tag."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"a {kind} with this key exists outside the run; nothing was written")


class AmbiguousResourceError(BridgeError):
    """A key matched more than one resource."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"more than one {kind} has this key; nothing was written")


@dataclass(frozen=True, slots=True)
class RunScope:
    run_id: str
    lab_tag: tuple[str, str] = DEFAULT_LAB_TAG

    @property
    def run_tag(self) -> tuple[str, str]:
        return (self.lab_tag[0] + RUN_TAG_SUFFIX, self.run_id)

    def system(self, system: str) -> str:
        return f"{system}/run/{self.run_id}"

    def tags(self) -> list[dict[str, str]]:
        return [{"system": s, "code": c} for s, c in (self.lab_tag, self.run_tag)]

    def owns(self, resource: dict[str, Any]) -> bool:
        system, code = self.run_tag
        return any(
            isinstance(t, dict) and t.get("system") == system and t.get("code") == code
            for t in (resource.get("meta") or {}).get("tag") or []
        )

    def tag_query(self) -> str:
        system, code = self.run_tag
        return "_tag=" + quote(f"{system}|{code}", safe=":/|")


def _swap_references(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {
            k: (new if k == "reference" and v == old else _swap_references(v, old, new))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_swap_references(v, old, new) for v in value]
    return value


def _scope_identifiers(identifiers: list[dict[str, Any]], scope: RunScope) -> list[dict[str, Any]]:
    keyed = [{**i, "system": scope.system(i["system"])} for i in identifiers]
    secondary = [{**i, "use": "secondary"} for i in identifiers]
    return keyed + secondary


def scope_body(
    mapped: MappedMessage, scope: RunScope, patient_id: str
) -> tuple[dict[str, Any], MappedMessage]:
    """The message's Bundle with the Patient entry taken out (referred to
    by ``Patient/<patient_id>``), identifiers namespaced by the run, and
    both tags on every resource. Returns the body and ``mapped`` with its
    Observation system namespaced too (retirement looks for that system)."""
    body = bundle_json(mapped)
    entries: list[dict[str, Any]] = body["entry"]
    patient = entries.pop(0)
    if patient["resource"]["resourceType"] != "Patient":  # pragma: no cover - mapper invariant
        raise AssertionError("the first entry is the Patient")
    for entry in entries:
        resource = _swap_references(entry["resource"], patient["fullUrl"], f"Patient/{patient_id}")
        resource["identifier"] = _scope_identifiers(resource["identifier"], scope)
        for based in resource.get("basedOn", []):
            ident = based.get("identifier")
            if isinstance(ident, dict) and "system" in ident:
                ident["system"] = scope.system(ident["system"])
        resource["meta"] = {"tag": scope.tags()}
        entry["resource"] = resource
    scoped = replace(
        mapped,
        observation_system=scope.system(mapped.observation_system)
        if mapped.observation_system
        else "",
    )
    return body, scoped


def lock_keys(body: dict[str, Any]) -> list[str]:
    """The keys of every entry (type, system, value), sorted, so locks are
    always taken in one order."""
    keys = set()
    for entry in body["entry"]:
        resource = entry["resource"]
        ident = resource["identifier"][0]
        keys.add(f"{resource['resourceType']}|{ident['system']}|{ident['value']}")
    return sorted(keys)


def _ensure_tags(resource: dict[str, Any], scope: RunScope) -> None:
    meta = resource.setdefault("meta", {})
    have = {(t.get("system"), t.get("code")) for t in meta.get("tag") or [] if isinstance(t, dict)}
    for tag in scope.tags():
        if (tag["system"], tag["code"]) not in have:
            meta.setdefault("tag", []).append(tag)


def prepare_run(
    client: HapiClient, mapped: MappedMessage, body: dict[str, Any], scope: RunScope
) -> set[int]:
    """Fill in each entry's request (see the module text) and add the
    retirements of a correction. ``body`` is from ``scope_body`` and is
    changed in place. Returns the positions of the planned creates."""
    entries: list[dict[str, Any]] = body["entry"]
    creates: set[int] = set()
    current_report: dict[str, Any] | None = None
    for n, entry in enumerate(entries):
        new = entry["resource"]
        kind = new["resourceType"]
        ident = new["identifier"][0]
        found = client.search_identifier(kind, ident["system"], ident["value"])
        if len(found) > 1:
            raise AmbiguousResourceError(kind)
        if not found:
            entry["request"] = {
                "method": "POST",
                "url": kind,
                "ifNoneExist": search_query(ident["system"], ident["value"])
                + "&"
                + scope.tag_query(),
            }
            creates.add(n)
            continue
        current = found[0]
        if not scope.owns(current):
            raise OutsideRunError(kind)
        if kind == "DiagnosticReport":
            check_not_stale(current, new)
            current_report = current
        if mapped.policies.get(entry["fullUrl"]) == MERGE:
            written = merge_resource(current, new)
        else:
            written = copy.deepcopy(new)
            written["meta"] = copy.deepcopy(current.get("meta") or {})
            written.get("meta", {}).pop("versionId", None)
            written.get("meta", {}).pop("lastUpdated", None)
        written["id"] = current["id"]
        _ensure_tags(written, scope)
        entry["resource"] = written
        entry["request"] = {
            "method": "PUT",
            "url": f"{kind}/{current['id']}",
            "ifMatch": _version_tag(current, kind),
        }
    if current_report is not None:
        keep = {
            f"{e['resource']['identifier'][0]['system']}|{e['resource']['identifier'][0]['value']}"
            for e in entries
            if e["resource"]["resourceType"] == "Observation"
        }
        entries.extend(retire_entries(client, mapped, current_report, keep, owned=scope.owns))
    return creates


def post_run(
    client: HapiClient, mapped: MappedMessage, body: dict[str, Any], scope: RunScope
) -> list[EntryResult]:
    """Prepare and commit one scoped message. Raises unless the server
    committed every entry and every planned create was a create."""
    creates = prepare_run(client, mapped, body, scope)
    if not body["entry"]:
        raise FhirPostError(0, "Bundle", reason="nothing to write")
    results = client.commit(body)
    check_created(body, results, creates)
    return results


__all__ = [
    "DEFAULT_LAB_TAG",
    "RUN_TYPES",
    "AmbiguousResourceError",
    "OutsideRunError",
    "RunScope",
    "check_run_id",
    "lock_keys",
    "post_run",
    "prepare_run",
    "scope_body",
]
