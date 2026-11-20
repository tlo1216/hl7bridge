"""Minimal FHIR REST client for HAPI.

Writes use conditional update (PUT <type>?identifier=system|value), which is
what makes re-running the bridge idempotent: zero matches creates (201), one
match updates in place (200), more than one is a server-side 412 that we
surface as an error instead of papering over.

Those query strings carry MRNs and accessions, so the client installs a
filter that rebuilds httpx and httpcore log records from safe fields only,
and FhirPostError never puts the query or the server body in its message.
Transport errors are raised ``from None``: the httpx exception text holds
the full URL, query included, and would print in any traceback.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.resource import Resource

from ..errors import FhirPostError, MappingError
from ..logsafety import install_third_party_redaction, safe_url
from ..v2fhir.common import check_key
from ..v2fhir.jsontext import dumps, loads, resource_dict

log = logging.getLogger(__name__)

FHIR_JSON = "application/fhir+json"
# A transaction-response entry status: "201 Created", "200 OK".
_ENTRY_STATUS = re.compile(r"([1-5][0-9]{2})(?: |$)")
# A response location: "Patient/12/_history/1", relative or absolute.
_LOCATION = re.compile(r"(?:^|/)([A-Z][A-Za-z]+)/([A-Za-z0-9.\-]{1,64})(?:/_history/[^/]+)?$")
_ID = re.compile(r"[A-Za-z0-9.\-]{1,64}")


@dataclass(frozen=True, slots=True)
class EntryResult:
    """One committed transaction entry: the resource type sent, the entry
    status (200 or 201), and the server id from its location, if given."""

    kind: str
    code: int
    server_id: str | None


class HapiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """extra_headers carries auth such as a hosted sandbox 'apikey'
        header; values never appear in logs or error messages."""
        install_third_party_redaction()
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Accept": FHIR_JSON, **(extra_headers or {})},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HapiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request. Transport failures (refused, reset, timeout) become a
        FhirPostError whose message names the failure type and the path only."""
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            err = FhirPostError(0, url, str(exc), reason=f"transport error ({type(exc).__name__})")
            err.transport = type(exc).__name__
        raise err from None

    def upsert(self, resource: Resource) -> tuple[str, str]:
        """Conditional update keyed on the resource's first identifier.

        Returns (server id, 'created' | 'updated').
        """
        identifiers = getattr(resource, "identifier", None)
        if not identifiers:
            raise FhirPostError(
                0, self.base_url, reason="resource has no identifier for conditional update"
            )
        ident = identifiers[0]
        resource_type = resource.get_resource_type()
        # The same rule as every v2fhir key: a value a token search would
        # split or misread is refused, never sent.
        check_key(str(ident.value or ""), f"{resource_type}.identifier")
        url = f"{self.base_url}/{resource_type}"
        params = {"identifier": f"{ident.system}|{ident.value}"}
        response = self._send(
            "PUT",
            url,
            params=params,
            content=dumps(resource_dict(resource)),
            headers={"Content-Type": FHIR_JSON},
        )
        if response.status_code not in (200, 201):
            raise FhirPostError(response.status_code, str(response.url), response.text)
        body = response.json()
        server_id = body.get("id")
        if not server_id:
            raise FhirPostError(
                response.status_code, str(response.url), reason="response carried no resource id"
            )
        return server_id, "created" if response.status_code == 201 else "updated"

    def transaction(self, bundle: Bundle) -> list[tuple[str, int]]:
        """POST a transaction Bundle model; (resource type, status) per
        entry. See ``commit``."""
        return [(r.kind, r.code) for r in self.commit(resource_dict(bundle))]

    def commit(
        self, body: dict[str, Any], *, ok_codes: tuple[int, ...] = (200, 201)
    ) -> list[EntryResult]:
        """POST a transaction Bundle (a dict from ``jsontext``) and check
        that all of it was committed.

        A 2xx status alone is not enough. The answer must be a Bundle of
        type ``transaction-response`` with exactly one entry per request
        entry, and every entry status must be in ``ok_codes`` (200 or 201
        unless the caller says otherwise; a delete answers 204). Anything else
        raises FhirPostError, whose text holds counts and positions only.
        Returns one EntryResult per entry, in request order.
        """
        requests: list[dict[str, Any]] = body.get("entry") or []
        response = self._send(
            "POST",
            self.base_url,
            content=dumps(body),
            headers={"Content-Type": FHIR_JSON},
        )
        url = str(response.url)
        if response.status_code != 200:
            raise FhirPostError(response.status_code, url, response.text)
        try:
            body = response.json()
        except ValueError:
            raise FhirPostError(200, url, reason="the answer is not JSON") from None
        if (
            not isinstance(body, dict)
            or body.get("resourceType") != "Bundle"
            or body.get("type") != "transaction-response"
        ):
            raise FhirPostError(200, url, reason="the answer is not a transaction-response Bundle")
        entries = body.get("entry")
        if not isinstance(entries, list) or len(entries) != len(requests):
            got = len(entries) if isinstance(entries, list) else 0
            raise FhirPostError(
                200, url, reason=f"the answer has {got} entries for {len(requests)} requests"
            )
        results: list[EntryResult] = []
        for i, (sent, answer) in enumerate(zip(requests, entries, strict=True), start=1):
            reply = answer.get("response") if isinstance(answer, dict) else None
            reply = reply if isinstance(reply, dict) else {}
            status = reply.get("status")
            match = _ENTRY_STATUS.match(status) if isinstance(status, str) else None
            code = int(match.group(1)) if match else 0
            if code not in ok_codes:
                raise FhirPostError(200, url, reason=f"entry {i} was not committed")
            resource = sent.get("resource") if isinstance(sent, dict) else None
            kind = str(resource.get("resourceType", "?")) if isinstance(resource, dict) else "?"
            if kind == "?" and isinstance(sent, dict):
                # A delete has no resource: the type is the first part of its URL.
                req_url = (sent.get("request") or {}).get("url", "")
                kind = str(req_url).split("/", 1)[0].split("?", 1)[0] or "?"
            location = reply.get("location")
            found = _LOCATION.search(location) if isinstance(location, str) else None
            server_id = found.group(2) if found and found.group(1) == kind else None
            results.append(EntryResult(kind, code, server_id))
        return results

    def search_identifier(
        self, resource_type: str, system: str, value: str
    ) -> list[dict[str, Any]]:
        """The resources whose identifier is exactly ``system|value``, at
        most two (enough to tell one from many). A paged answer counts as
        many. Decimals are read as Decimal, so a resource written back
        keeps them. The value passes the key check first."""
        try:
            check_key(value, f"{resource_type}.identifier")
        except MappingError as exc:
            raise FhirPostError(0, resource_type, reason=str(exc)) from None
        # no-cache: HAPI reuses a recent search result by default, and a
        # merge must see what is on the server now, not a minute ago.
        response = self._send(
            "GET",
            f"{self.base_url}/{resource_type}",
            params={"identifier": f"{system}|{value}", "_count": "2"},
            headers={"Cache-Control": "no-cache"},
        )
        if response.status_code != 200:
            raise FhirPostError(response.status_code, str(response.url), response.text)
        try:
            body = loads(response.content)
        except ValueError:
            raise FhirPostError(200, str(response.url), reason="the answer is not JSON") from None
        if not isinstance(body, dict) or body.get("resourceType") != "Bundle":
            raise FhirPostError(200, str(response.url), reason="the answer is not a Bundle")
        found = [
            e["resource"]
            for e in body.get("entry") or []
            if isinstance(e, dict)
            and isinstance(e.get("resource"), dict)
            and e["resource"].get("resourceType") == resource_type
        ]
        paged = any(
            isinstance(link, dict) and link.get("relation") == "next"
            for link in body.get("link") or []
        )
        if paged and len(found) < 2:
            found = found * 2  # more on a next page: at least two, never "the first"
        return found

    def search_bundle(self, resource_type: str, params: dict[str, str]) -> dict[str, Any]:
        """One search page as a Bundle dict (decimals kept), read fresh
        (no-cache). Raises FhirPostError for any answer that is not one."""
        response = self._send(
            "GET",
            f"{self.base_url}/{resource_type}",
            params=params,
            headers={"Cache-Control": "no-cache"},
        )
        return self._bundle(response)

    def _bundle(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200:
            raise FhirPostError(response.status_code, str(response.url), response.text)
        try:
            body = loads(response.content)
        except ValueError:
            raise FhirPostError(200, str(response.url), reason="the answer is not JSON") from None
        if not isinstance(body, dict) or body.get("resourceType") != "Bundle":
            raise FhirPostError(200, str(response.url), reason="the answer is not a Bundle")
        return body

    def search_all(
        self, resource_type: str, params: dict[str, str], *, max_pages: int = 100
    ) -> list[dict[str, Any]]:
        """Every resource of ``resource_type`` a search returns, across pages.

        A ``next`` link is followed only when it points under this client's
        base URL; any other link, or more than ``max_pages`` pages, raises
        FhirPostError, so a partial list is never taken for the whole."""
        body = self.search_bundle(resource_type, params)
        found: list[dict[str, Any]] = []
        for _ in range(max_pages):
            found.extend(
                e["resource"]
                for e in body.get("entry") or []
                if isinstance(e, dict)
                and isinstance(e.get("resource"), dict)
                and e["resource"].get("resourceType") == resource_type
            )
            nxt = next(
                (
                    link.get("url")
                    for link in body.get("link") or []
                    if isinstance(link, dict) and link.get("relation") == "next"
                ),
                None,
            )
            if nxt is None:
                return found
            if not isinstance(nxt, str) or not nxt.startswith(
                (self.base_url + "?", self.base_url + "/")
            ):
                raise FhirPostError(
                    200, resource_type, reason="a next page link points off this server"
                )
            response = self._send("GET", nxt, headers={"Cache-Control": "no-cache"})
            body = self._bundle(response)
        raise FhirPostError(200, resource_type, reason=f"more than {max_pages} result pages")

    def read_by_id(self, resource_type: str, server_id: str) -> dict[str, Any] | None:
        """``GET <type>/<id>``; None when the server has no such resource."""
        if not _ID.fullmatch(server_id):
            raise FhirPostError(0, resource_type, reason="not a FHIR resource id")
        response = self._send("GET", f"{self.base_url}/{resource_type}/{server_id}")
        if response.status_code in (404, 410):
            return None
        if response.status_code != 200:
            raise FhirPostError(response.status_code, str(response.url), response.text)
        try:
            body = loads(response.content)
        except ValueError:
            raise FhirPostError(200, str(response.url), reason="the answer is not JSON") from None
        if not isinstance(body, dict) or body.get("resourceType") != resource_type:
            raise FhirPostError(
                200, str(response.url), reason=f"the answer is not a {resource_type}"
            )
        return body

    def search_total(self, resource_type: str, params: dict[str, str] | None = None) -> int:
        """Server-side count. _summary=count means no resources transfer."""
        query: dict[str, str] = {"_summary": "count", **(params or {})}
        response = self._send(
            "GET",
            f"{self.base_url}/{resource_type}",
            params=query,
            headers={"Cache-Control": "no-cache"},  # a count of now, not of a cached search
        )
        if response.status_code != 200:
            raise FhirPostError(response.status_code, str(response.url), response.text)
        body = response.json()
        total = body.get("total")
        if not isinstance(total, int):
            raise FhirPostError(
                response.status_code,
                str(response.url),
                response.text,
                reason="no integer total in search response",
            )
        return total

    def read(self, resource_type: str, params: dict[str, str]) -> dict[str, Any]:
        """Search and return the raw bundle JSON."""
        response = self._send("GET", f"{self.base_url}/{resource_type}", params=params)
        if response.status_code != 200:
            raise FhirPostError(response.status_code, str(response.url), response.text)
        result: dict[str, Any] = response.json()
        return result

    def metadata_status(self) -> int:
        """HTTP status of GET /metadata, or 0 when the server never answered."""
        try:
            response = self._client.get(f"{self.base_url}/metadata")
        except httpx.TransportError as exc:
            # Probe failure is an expected outcome, but never a silent one.
            log.warning(
                "FHIR /metadata probe failed for %s: %s",
                safe_url(self.base_url),
                type(exc).__name__,
            )
            return 0
        return response.status_code

    def metadata_ok(self) -> bool:
        return self.metadata_status() == 200

    def capability_summary(self) -> dict[str, Any]:
        """Read-only probe: software name/version and FHIR version from the
        CapabilityStatement, plus the HTTP status. Raises FhirPostError on
        transport failure so callers see exactly what broke."""
        try:
            response = self._client.get(f"{self.base_url}/metadata")
        except httpx.TransportError as exc:
            raise FhirPostError(
                0,
                f"{self.base_url}/metadata",
                str(exc),
                reason=f"transport error ({type(exc).__name__})",
            ) from None
        summary: dict[str, Any] = {"status_code": response.status_code}
        if response.status_code == 200:
            body = response.json()
            software = body.get("software", {})
            summary["software"] = software.get("name")
            summary["software_version"] = software.get("version")
            summary["fhir_version"] = body.get("fhirVersion")
        else:
            summary["body"] = response.text[:300]
        return summary
