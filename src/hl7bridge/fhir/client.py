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
from typing import Any

import httpx
from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.resource import Resource

from ..errors import FhirPostError
from ..logsafety import install_third_party_redaction, safe_url

log = logging.getLogger(__name__)

FHIR_JSON = "application/fhir+json"
# A transaction-response entry status: "201 Created", "200 OK".
_ENTRY_STATUS = re.compile(r"([1-5][0-9]{2})(?: |$)")


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
            raise FhirPostError(
                0, url, str(exc), reason=f"transport error ({type(exc).__name__})"
            ) from None

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
        url = f"{self.base_url}/{resource_type}"
        params = {"identifier": f"{ident.system}|{ident.value}"}
        response = self._send(
            "PUT",
            url,
            params=params,
            content=resource.model_dump_json(exclude_none=True),
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
        """POST a transaction Bundle and check that all of it was committed.

        A 2xx status alone is not enough. The answer must be a Bundle of
        type ``transaction-response`` with exactly one entry per request
        entry, and every entry status must be 200 or 201. Anything else
        raises FhirPostError, whose text holds counts and positions only.
        Returns (resource type, status code) per entry, in request order.
        """
        requests = bundle.entry or []
        response = self._send(
            "POST",
            self.base_url,
            content=bundle.model_dump_json(exclude_none=True),
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
        results: list[tuple[str, int]] = []
        for i, (sent, answer) in enumerate(zip(requests, entries, strict=True), start=1):
            status = answer.get("response", {}).get("status") if isinstance(answer, dict) else None
            match = _ENTRY_STATUS.match(status) if isinstance(status, str) else None
            code = int(match.group(1)) if match else 0
            if code not in (200, 201):
                raise FhirPostError(200, url, reason=f"entry {i} was not committed")
            kind = sent.resource.get_resource_type() if sent.resource else "?"
            results.append((kind, code))
        return results

    def search_total(self, resource_type: str, params: dict[str, str] | None = None) -> int:
        """Server-side count. _summary=count means no resources transfer."""
        query: dict[str, str] = {"_summary": "count", **(params or {})}
        response = self._send("GET", f"{self.base_url}/{resource_type}", params=query)
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
