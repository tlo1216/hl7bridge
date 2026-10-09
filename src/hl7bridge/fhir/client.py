"""Minimal FHIR REST client for HAPI.

Writes use conditional update (PUT <type>?identifier=system|value), which is
what makes re-running the bridge idempotent: zero matches creates (201), one
match updates in place (200), more than one is a server-side 412 that we
surface as an error instead of papering over.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fhir.resources.R4B.resource import Resource

from ..errors import FhirPostError

log = logging.getLogger(__name__)

FHIR_JSON = "application/fhir+json"


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

    def upsert(self, resource: Resource) -> tuple[str, str]:
        """Conditional update keyed on the resource's first identifier.

        Returns (server id, 'created' | 'updated').
        """
        identifiers = getattr(resource, "identifier", None)
        if not identifiers:
            raise FhirPostError(
                0, self.base_url, "resource has no identifier for conditional update"
            )
        ident = identifiers[0]
        resource_type = resource.get_resource_type()
        url = f"{self.base_url}/{resource_type}"
        params = {"identifier": f"{ident.system}|{ident.value}"}
        response = self._client.put(
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
                response.status_code, str(response.url), "response carried no resource id"
            )
        return server_id, "created" if response.status_code == 201 else "updated"

    def search_total(self, resource_type: str, params: dict[str, str] | None = None) -> int:
        """Server-side count. _summary=count means no resources transfer."""
        query: dict[str, str] = {"_summary": "count", **(params or {})}
        response = self._client.get(f"{self.base_url}/{resource_type}", params=query)
        if response.status_code != 200:
            raise FhirPostError(response.status_code, str(response.url), response.text)
        body = response.json()
        total = body.get("total")
        if not isinstance(total, int):
            raise FhirPostError(
                response.status_code, str(response.url), f"no integer total in {body!r}"
            )
        return total

    def read(self, resource_type: str, params: dict[str, str]) -> dict[str, Any]:
        """Search and return the raw bundle JSON."""
        response = self._client.get(f"{self.base_url}/{resource_type}", params=params)
        if response.status_code != 200:
            raise FhirPostError(response.status_code, str(response.url), response.text)
        result: dict[str, Any] = response.json()
        return result

    def metadata_ok(self) -> bool:
        try:
            response = self._client.get(f"{self.base_url}/metadata")
        except httpx.TransportError as exc:
            # Probe failure is an expected outcome, but never a silent one.
            log.warning("FHIR /metadata probe failed for %s: %s", self.base_url, exc)
            return False
        return response.status_code == 200

    def capability_summary(self) -> dict[str, Any]:
        """Read-only probe: software name/version and FHIR version from the
        CapabilityStatement, plus the HTTP status. Raises FhirPostError on
        transport failure so callers see exactly what broke."""
        try:
            response = self._client.get(f"{self.base_url}/metadata")
        except httpx.TransportError as exc:
            raise FhirPostError(0, f"{self.base_url}/metadata", str(exc)) from exc
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
