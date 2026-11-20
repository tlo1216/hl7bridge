"""A small in-memory FHIR server for unit tests, behind httpx.MockTransport.

It does what hl7bridge's client asks of a server and no more: search by
exact identifier, read by id, conditional update (``PUT
<Type>?identifier=``), and transactions with conditional updates,
conditional creates (``ifNoneExist``), updates by id with ``If-Match``,
and ``urn:uuid`` references resolved to ``<Type>/<id>``. A failed
``If-Match`` or a conditional update that matches more than one resource
fails the whole transaction, as on HAPI. Decimals are kept as Decimal.
Searches and conditional creates honour ``_tag``. Transactions take
``DELETE <Type>/<id>`` too, refused (and the whole transaction with them)
when a resource that stays still refers to the one deleted, as HAPI's
referential integrity check does.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from hl7bridge.v2fhir.jsontext import dumps, loads


def _response(status: int, body: Any) -> httpx.Response:
    return httpx.Response(
        status, content=dumps(body).encode(), headers={"Content-Type": "application/fhir+json"}
    )


def _outcome(status: int, code: str) -> httpx.Response:
    return _response(
        status, {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": code}]}
    )


class FakeFhir:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}
        self.requests: list[tuple[str, str]] = []
        self.transactions: list[dict[str, Any]] = []
        self._ids = itertools.count(1)
        # Set to a callable(body) -> httpx.Response | None to interfere.
        self.before_commit: Any = None

    # -- data helpers -----------------------------------------------------------

    def all(self, kind: str) -> list[dict[str, Any]]:
        return list(self.data.get(kind, {}).values())

    def by_identifier(self, kind: str, system: str, value: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self.all(kind)
            if any(
                i.get("system") == system and i.get("value") == value
                for i in r.get("identifier", [])
            )
        ]

    def by_tag(self, kind: str, tag: str) -> list[dict[str, Any]]:
        system, _, code = tag.partition("|")
        return [
            r
            for r in self.all(kind)
            if any(
                t.get("system") == system and t.get("code") == code
                for t in r.get("meta", {}).get("tag", [])
            )
        ]

    def search(self, kind: str, params: dict[str, str]) -> list[dict[str, Any]]:
        if "identifier" in params:
            system, _, value = params["identifier"].partition("|")
            found = self.by_identifier(kind, system, value)
        else:
            found = self.all(kind)
        if "_tag" in params:
            tagged = {r["id"] for r in self.by_tag(kind, params["_tag"])}
            found = [r for r in found if r["id"] in tagged]
        return found

    def by_key(self, kind: str, key: str) -> dict[str, Any]:
        system, _, value = key.partition("|")
        found = self.by_identifier(kind, system, value)
        assert len(found) == 1, (kind, key, len(found))
        return found[0]

    def _write(self, kind: str, server_id: str, resource: dict[str, Any]) -> str:
        current = self.data.setdefault(kind, {}).get(server_id)
        version = int(current["meta"]["versionId"]) + 1 if current else 1
        stored = copy.deepcopy(resource)
        stored["id"] = server_id
        stored["meta"] = {**stored.get("meta", {}), "versionId": str(version)}
        self.data[kind][server_id] = stored
        return str(version)

    # -- HTTP -------------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        parts = path.split("/")
        self.requests.append((request.method, str(request.url)))
        if path.endswith("/metadata"):
            return _response(200, {"resourceType": "CapabilityStatement"})
        if request.method == "POST" and parts[-1] == "fhir":
            return self._transaction(loads(request.content))
        if request.method == "GET" and len(parts) >= 2 and parts[-2][:1].isupper():
            kind, server_id = parts[-2], parts[-1]
            found = self.data.get(kind, {}).get(server_id)
            return _response(200, found) if found else _outcome(404, "not-found")
        kind = parts[-1]
        params = dict(request.url.params)
        if request.method == "GET":
            found = self.search(kind, params)
            if params.get("_summary") == "count":
                return _response(200, {"resourceType": "Bundle", "total": len(found)})
            count = int(params.get("_count", "100"))
            return _response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"resource": r} for r in found[:count]],
                },
            )
        if request.method == "PUT":
            system, _, value = params["identifier"].partition("|")
            found = self.by_identifier(kind, system, value)
            if len(found) > 1:
                return _outcome(412, "multiple-matches")
            server_id = found[0]["id"] if found else str(next(self._ids))
            self._write(kind, server_id, loads(request.content))
            return _response(201 if not found else 200, self.data[kind][server_id])
        return _outcome(400, "not-supported")

    def _transaction(self, body: dict[str, Any]) -> httpx.Response:
        self.transactions.append(copy.deepcopy(body))
        if self.before_commit is not None:
            answer = self.before_commit(body)
            if answer is not None:
                return answer
        saved = copy.deepcopy(self.data)
        ids = itertools.count(next(self._ids))
        plan: list[tuple[str, str, int, dict[str, Any] | None]] = []
        placeholders: dict[str, str] = {}
        deletes: list[tuple[str, str]] = []
        for entry in body["entry"]:
            req = entry["request"]
            if req["method"] == "DELETE" and "?" in req["url"]:
                # Conditional delete (``?_id=&_tag=``): no match deletes nothing.
                kind, _, query = req["url"].partition("?")
                params = {k: v[0] for k, v in parse_qs(query).items()}
                server_id = params.pop("_id")
                if server_id in {r["id"] for r in self.search(kind, params)}:
                    deletes.append((kind, server_id))
            elif req["method"] == "DELETE":
                kind, _, server_id = req["url"].partition("/")
                if server_id not in self.data.get(kind, {}):
                    return _outcome(404, "not-found")
                deletes.append((kind, server_id))
        n_deletes = sum(1 for e in body["entry"] if e["request"]["method"] == "DELETE")
        if n_deletes:
            gone = {f"{k}/{i}" for k, i in deletes}
            for kind, resources in self.data.items():
                for server_id, resource in resources.items():
                    if f"{kind}/{server_id}" not in gone and _refers_to(resource, gone):
                        return _outcome(409, "conflict")
            for kind, server_id in deletes:
                del self.data[kind][server_id]
            answers = [{"response": {"status": "204 No Content"}} for _ in range(n_deletes)]
            return _response(
                200, {"resourceType": "Bundle", "type": "transaction-response", "entry": answers}
            )
        for entry in body["entry"]:
            req, resource = entry["request"], entry["resource"]
            kind = resource["resourceType"]
            url = urlsplit(req["url"])
            if req["method"] == "POST":
                query = {k: v[0] for k, v in parse_qs(req.get("ifNoneExist", "")).items()}
                found = self.search(kind, query)
                if found:
                    plan.append((kind, found[0]["id"], 200, None))
                else:
                    plan.append((kind, str(next(ids)), 201, resource))
            elif "?" in req["url"]:
                system, _, value = parse_qs(url.query)["identifier"][0].partition("|")
                found = self.by_identifier(kind, system, value)
                if len(found) > 1:
                    self.data = saved
                    return _outcome(412, "multiple-matches")
                server_id = found[0]["id"] if found else str(next(ids))
                plan.append((kind, server_id, 200 if found else 201, resource))
            else:
                server_id = url.path.split("/")[-1]
                current = self.data.get(kind, {}).get(server_id)
                wanted = req.get("ifMatch")
                if current is None or (
                    wanted is not None and wanted != f'W/"{current["meta"]["versionId"]}"'
                ):
                    self.data = saved
                    return _outcome(412, "conflict")
                plan.append((kind, server_id, 200, resource))
            placeholders[entry["fullUrl"]] = f"{kind}/{plan[-1][1]}"
        self._ids = ids
        answers = []
        for kind, server_id, status, resource in plan:
            version = self.data.get(kind, {}).get(server_id, {}).get("meta", {}).get("versionId")
            if resource is not None:
                version = self._write(kind, server_id, _resolve(resource, placeholders))
            word = "Created" if status == 201 else "OK"
            answers.append(
                {
                    "response": {
                        "status": f"{status} {word}",
                        "location": f"{kind}/{server_id}/_history/{version}",
                    }
                }
            )
        return _response(
            200, {"resourceType": "Bundle", "type": "transaction-response", "entry": answers}
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _refers_to(value: Any, targets: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            (k == "reference" and v in targets) or _refers_to(v, targets) for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_refers_to(v, targets) for v in value)
    return False


def _resolve(value: Any, placeholders: dict[str, str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "reference" and isinstance(item, str) and item in placeholders:
                out[key] = placeholders[item]
            else:
                out[key] = _resolve(item, placeholders)
        return out
    if isinstance(value, list):
        return [_resolve(v, placeholders) for v in value]
    return value
