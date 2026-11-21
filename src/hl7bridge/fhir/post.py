"""Post mapped messages to a FHIR server. ``to-fhir --post`` and ``fhir
sync`` both write through here, so they write the same content.

Each message is one transaction Bundle from ``hl7bridge.v2fhir``. Before it
is sent, two things are read from the server and folded into it:

1. **Merge** (entries with the ``merge`` policy: Patient from ADT,
   Encounter, ServiceRequest; ADT^A40 adds the merged-away Patient). In
   HL7 v2 an empty field means "no
   change", so a thin A08 must not erase the address an A01 wrote. The
   current resource is read by its identifier and the message's elements
   are laid over it: each element the mapper owns replaces the current one
   when the message has it and is kept when it does not. The write goes
   to ``<Type>/<id>`` with ``If-Match`` on the version read, so a change
   made between the read and the write fails the transaction instead of
   being lost. No current resource: a conditional create (``POST`` with
   ``ifNoneExist``) makes it, and an answer of 200 instead of 201 (another
   writer created it between the search and the write) fails the post
   with a "send again" reason, so a concurrent create is never overwritten
   unmerged. More than one: refused, never "first match wins".
   ADT^A40 adds rules, all checked before anything is sent (``MergeRefusedError``):
   Patient.link keeps what the server has; a merged-away Patient already
   replaced by another Patient, a survivor that is inactive or merged away
   itself (a reversed merge), and two entries that resolve to one server
   resource are refused. A resend of a merge already there passes. If another
   writer creates either Patient between the read and the commit, the
   conditional create answers 200, the other Patient's write has committed,
   and ``ConcurrentWriteError`` says the merge may be partly applied and to
   send again (a resend is safe).
2. **Retire** (ORU). A corrected report can drop a result. The current
   DiagnosticReport's ``result`` list is read, and each Observation in it
   whose key parses to this report's accession and that the new version no
   longer carries is written back with status ``entered-in-error`` (with
   ``If-Match``) in the same transaction. Observations with another
   system or another report's key are never touched.

3. **Stale results** (ORU). A report older than the one on the server (by
   OBR-22, then by status, ``is_older``) raises StaleResultError and
   nothing is sent. ``check_run_order`` applies the same rule inside one
   run, before anything is posted. A retraction (cancelled,
   entered-in-error) ranks above a correction, so a correction with the
   same or no OBR-22 cannot bring a retracted report back. With no OBR-22
   on either side and the same rank, the message is taken as the newer
   (ADR-0004: MSH-7 is not used).

A profile with ``patient_policy: match_only`` is refused here: its
Patients belong to someone else and this path writes Patients.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..errors import EXIT_FINDINGS, BridgeError, ConfigError, FhirPostError
from ..v2fhir.bundle import MERGE, MERGE_EVENT, MappedMessage, bundle_json, full_url, search_query
from ..v2fhir.observation import KEY_JOIN, SET_ID_PART, SUB_ID_PART
from .client import EntryResult, HapiClient

# The elements each mapper writes and so owns in a merge. Everything else
# on the current resource (meta, tags, other identifiers, extensions) stays.
OWNED: dict[str, tuple[str, ...]] = {
    "Patient": ("name", "gender", "birthDate", "address", "telecom", "active", "link"),
    "Encounter": ("status", "class", "subject", "period"),
    "ServiceRequest": ("status", "intent", "code", "authoredOn", "subject"),
}
# Elements kept whole and added to: Patient.link entries on the server
# stay, and the message's are added when not already there. Names, addresses
# and telephone numbers are not here: the message carries the sender's whole
# current list, so they are owned and replaced whole.
ADDITIVE = {("Patient", "link")}
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
    # A retraction outranks a correction: it is the last word on a report.
    "cancelled": 4,
    "entered-in-error": 4,
}
_REFERENCE = re.compile(r"(?:^|/)Observation/([A-Za-z0-9.\-]{1,64})(?:/_history/[^/]+)?$")


class PostOrderError(BridgeError):
    """A run would post an older version of a report after a newer one."""


class StaleResultError(BridgeError):
    """The server holds a newer version of the report than the message."""

    def __init__(self) -> None:
        super().__init__(
            "the server holds a newer version of this report (OBR-22, then OBR-25);"
            " the older one was not posted.",
            hint="Send the newest version, or leave the older one out.",
        )


class ConcurrentWriteError(FhirPostError):
    """Another writer created a resource between our search and our write."""

    exit_code = EXIT_FINDINGS  # the server answered; a resend is the remedy

    def __init__(self, kind: str, *, partial: bool = False) -> None:
        reason = f"another writer created the {kind} first; nothing of it was merged, send again"
        if partial:
            reason = (
                f"another writer created the {kind} while the merge was posted; the merge may"
                " be only partly applied (the other Patient may already be updated, or the"
                " link missing); send again (a resend is safe)"
            )
        super().__init__(0, kind, reason=reason)


class MergeRefusedError(FhirPostError):
    """An ADT^A40 that the server's current state does not allow, or that
    two entries resolve to one resource. Raised while preparing, so nothing
    was written; the reason is fixed text."""

    exit_code = EXIT_FINDINGS  # a refusal of the message, not a connection failure

    def __init__(self, reason: str) -> None:
        super().__init__(0, "Patient", reason=reason)


def parse_instant(value: object) -> datetime | None:
    """A FHIR instant or dateTime from JSON or a model, or None."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def is_older(
    new_issued: datetime | None, new_status: str, cur_issued: datetime | None, cur_status: str
) -> bool:
    """True when a report version (issued, status) is older than the
    current one: an earlier OBR-22, or with the same or no OBR-22 a lower
    status rank. The same version is not older, so a resend passes."""
    if new_issued is not None and cur_issued is not None and new_issued != cur_issued:
        return new_issued < cur_issued
    return STATUS_RANK.get(new_status, 0) < STATUS_RANK.get(cur_status, 0)


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
    """``system|value`` of the first identifier of a resource the mapper
    built (the key is always first there). Never used on a Patient read
    from the server, which lists identifiers in any order."""
    ident = resource["identifier"][0]
    return f"{ident.get('system', '')}|{ident['value']}"


def _retired_key(resource: dict[str, Any], system: str) -> str:
    """The key of a retired Observation: its identifier in the report's
    Observation ``system``, not whichever one the server lists first."""
    for ident in resource.get("identifier", []):
        if ident.get("system") == system and ident.get("value"):
            return f"{system}|{ident['value']}"
    return _key(resource)


def _link_key(link: dict[str, Any], resolved: dict[str, str]) -> tuple[Any, Any]:
    ref = str(link.get("other", {}).get("reference", ""))
    return link.get("type"), _patient_ref(resolved.get(ref, ref)) or ref


def _identifier_key(ident: dict[str, Any]) -> tuple[Any, Any, Any]:
    """An identifier's system and value; with no system, its type code and
    assigning authority too, so two systemless numbers of different types or
    from different authorities stay apart."""
    system = ident.get("system")
    if system:
        return system, ident.get("value"), None
    codings = ident.get("type", {}).get("coding") or [{}]
    extra = (codings[0].get("code"), ident.get("assigner", {}).get("display"))
    return None, ident.get("value"), extra


def merge_resource(
    current: dict[str, Any],
    new: dict[str, Any],
    resolved: dict[str, str] | None = None,
    clear: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """``new`` laid over ``current``: owned elements the message has replace
    the current ones, absent ones are kept, identifiers are added to, and
    additive elements (Patient.link) keep the current entries and gain the
    message's that are not there yet. ``resolved`` maps a ``urn:uuid`` full
    URL of this transaction to its ``Patient/<id>``, for comparing links.
    ``clear`` names owned elements the message explicitly nulled (the HL7
    null): removed from the result when the message gives no value for them."""
    kind = new["resourceType"]
    merged = copy.deepcopy(current)
    ids = resolved or {}
    for element in OWNED[kind]:
        if element not in new:
            if element in clear and (kind, element) not in ADDITIVE:
                merged.pop(element, None)
            continue
        if (kind, element) in ADDITIVE:
            have_links = {_link_key(x, ids) for x in merged.get(element, [])}
            for item in new[element]:
                if _link_key(item, ids) not in have_links:
                    added = copy.deepcopy(item)
                    ref = str(added.get("other", {}).get("reference", ""))
                    if ref in ids:
                        # Written as the server's id, not left for it to resolve.
                        added["other"]["reference"] = ids[ref]
                    merged.setdefault(element, []).append(added)
        elif (kind, element) in NESTED and isinstance(merged.get(element), dict):
            merged[element] = {**merged[element], **new[element]}
        else:
            merged[element] = copy.deepcopy(new[element])
    have = {_identifier_key(i) for i in merged.get("identifier", [])}
    for ident in new.get("identifier", []):
        if _identifier_key(ident) not in have:
            have.add(_identifier_key(ident))
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


_PATIENT_REF = re.compile(r"(?:^|/)(Patient/[A-Za-z0-9.\-]{1,64})(?:/_history/[^/]+)?$")


def _patient_ref(reference: str) -> str | None:
    """``Patient/<id>`` from a literal or absolute Patient reference."""
    match = _PATIENT_REF.search(reference)
    return match.group(1) if match else None


def _replaced_by(patient: dict[str, Any]) -> list[str]:
    """The targets (``Patient/<id>``, or the raw reference) of the
    replaced-by links of a Patient."""
    out = []
    for link in patient.get("link", []):
        if link.get("type") == "replaced-by":
            ref = str(link.get("other", {}).get("reference", ""))
            out.append(_patient_ref(ref) or ref)
    return out


def _check_merge(survivor: dict[str, Any] | None, gone: dict[str, Any] | None) -> None:
    """Refuse an A40 the server's current state contradicts: the merged-away
    Patient already replaced by another one, or a surviving Patient that is
    itself inactive or merged away (a reversed merge would leave two
    Patients pointing at each other). A resend of the merge that is already
    there passes."""
    target = f"Patient/{survivor['id']}" if survivor else None
    if gone is not None:
        replaced = _replaced_by(gone)
        if target is not None and replaced == [target]:
            return
        if replaced:
            raise MergeRefusedError(
                "the merged-away Patient is already replaced by another Patient;"
                " a contradicting merge is not applied"
            )
    if survivor is not None and (survivor.get("active") is False or _replaced_by(survivor)):
        raise MergeRefusedError(
            "the surviving Patient is inactive or merged away itself; the merge is not applied"
        )


def _merge_entry(
    client: HapiClient,
    entry: dict[str, Any],
    current: dict[str, Any] | None,
    resolved: dict[str, str],
    clear: frozenset[str] = frozenset(),
) -> None:
    new = entry["resource"]
    kind = new["resourceType"]
    if current is None:
        # Create only if it is still missing at commit; post_mapped checks
        # that the answer is 201.
        ident = new["identifier"][0]
        entry["request"] = {
            "method": "POST",
            "url": kind,
            "ifNoneExist": search_query(ident["system"], ident["value"]),
        }
        return
    merged = merge_resource(current, new, resolved, clear)
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


def check_not_stale(current: dict[str, Any] | None, report: dict[str, Any]) -> None:
    """Raise StaleResultError when ``report`` is older than ``current``."""
    if current is None:
        return
    if is_older(
        parse_instant(report.get("issued")),
        str(report.get("status", "")),
        parse_instant(current.get("issued")),
        str(current.get("status", "")),
    ):
        raise StaleResultError


def retire_entries(
    client: HapiClient,
    mapped: MappedMessage,
    current: dict[str, Any],
    keep: set[str],
    *,
    owned: Callable[[dict[str, Any]], bool] = lambda _obs: True,
) -> list[dict[str, Any]]:
    """Entries that set to entered-in-error each result of ``current`` (the
    report on the server) that is this report's own and that ``keep``
    (the new version's result keys) no longer holds. ``owned`` can narrow
    "own" further (direct mode: the run's tag)."""
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
        if not owned(obs):
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
    merging = [e for e in entries if mapped.policies.get(e["fullUrl"]) == MERGE]
    currents = {
        e["fullUrl"]: _one(client, e["resource"]["resourceType"], _key(e["resource"]))
        for e in merging
    }
    seen: set[tuple[str, str]] = set()
    resolved: dict[str, str] = {}
    for entry in merging:
        found = currents[entry["fullUrl"]]
        if found is not None:
            slot = (entry["resource"]["resourceType"], str(found["id"]))
            if slot in seen:
                raise MergeRefusedError(
                    "two entries of the message resolve to one resource on the server;"
                    " nothing was written"
                )
            seen.add(slot)
            resolved[entry["fullUrl"]] = f"{slot[0]}/{slot[1]}"
    if mapped.message_type == MERGE_EVENT:
        survivor, gone = (currents[e["fullUrl"]] for e in merging[:2])
        _check_merge(survivor, gone)
    for entry in merging:
        clear = mapped.clears.get(entry["fullUrl"], frozenset())
        _merge_entry(client, entry, currents[entry["fullUrl"]], resolved, clear)
    report = next(
        (e["resource"] for e in entries if e["resource"]["resourceType"] == "DiagnosticReport"),
        None,
    )
    if report is not None:
        current = _one(client, "DiagnosticReport", _key(report))
        check_not_stale(current, report)
        if current is not None:
            keep = {
                _key(e["resource"])
                for e in entries
                if e["resource"]["resourceType"] == "Observation"
            }
            entries.extend(retire_entries(client, mapped, current, keep))
    return body


def check_created(
    body: dict[str, Any],
    results: Sequence[EntryResult],
    creates: set[int],
    *,
    merge_event: bool = False,
) -> None:
    """Raise ConcurrentWriteError when an entry planned as a create (its
    search found nothing) was answered 200: another writer made it first."""
    for n in creates:
        if results[n].code != 201:
            kind = str(body["entry"][n]["resource"]["resourceType"])
            # An A40 writes both Patients in one transaction, so a lost
            # create of either leaves the other updated.
            raise ConcurrentWriteError(kind, partial=merge_event)


def refuse_match_only(hospital_id: str, policy: str) -> None:
    """ConfigError for a match_only profile: the batch path creates and
    merges Patients, which such a profile says belong to someone else."""
    if policy == "match_only":
        raise ConfigError(
            f"profile {hospital_id} has patient_policy match_only; to-fhir --post and fhir"
            " sync create and merge Patients, so they refuse it.",
            hint="Use listen --target fhir for a match_only profile, or a profile with"
            " patient_policy create on a server of your own.",
        )


def post_mapped(client: HapiClient, mapped: MappedMessage) -> list[Committed]:
    """Prepare and post one message. Raises FhirPostError unless the server
    committed every entry (``HapiClient.commit``), and ConfigError for a
    match_only profile before anything is read or sent."""
    refuse_match_only(mapped.hospital_id, mapped.patient_policy)
    body = prepare(client, mapped)
    creates = {
        n
        for n, entry in enumerate(body["entry"])
        if mapped.policies.get(entry["fullUrl"]) == MERGE and entry["request"]["method"] == "POST"
    }
    results: list[EntryResult] = client.commit(body)
    check_created(body, results, creates, merge_event=mapped.message_type == MERGE_EVENT)
    mapped_count = len(mapped.bundle.entry or [])
    # A merged resource keeps the server's identifier order, so a key is read
    # from the mapped message (its first identifier is the key), not the merge.
    keys = [_key(e["resource"]) for e in bundle_json(mapped)["entry"]]
    return [
        Committed(
            r.kind,
            keys[n]
            if n < mapped_count
            else _retired_key(entry["resource"], mapped.observation_system),
            r.code,
            r.server_id,
            retired=n >= mapped_count,
        )
        for n, (r, entry) in enumerate(zip(results, body["entry"], strict=True))
    ]


@dataclass(frozen=True, slots=True)
class _Version:
    label: str
    status: str
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
        version = _Version(label, str(report.status), issued)  # type: ignore[attr-defined]
        before = latest.get(key)
        if before is not None and is_older(issued, version.status, before.issued, before.status):
            raise PostOrderError(
                f"{label} holds an older version of the same report as {before.label}"
                " (OBR-22, then OBR-25); posting it after would undo the newer one.",
                hint="Post the messages in the order they were sent, or leave the older one out.",
            )
        latest[key] = version


__all__ = [
    "OWNED",
    "Committed",
    "ConcurrentWriteError",
    "MergeRefusedError",
    "PostOrderError",
    "StaleResultError",
    "check_created",
    "check_not_stale",
    "check_run_order",
    "is_older",
    "merge_resource",
    "parse_instant",
    "post_mapped",
    "prepare",
    "refuse_match_only",
    "result_accession",
    "retire_entries",
    "search_query",
]
