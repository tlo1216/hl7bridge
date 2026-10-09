"""HapiClient against a mock transport: conditional-update semantics, loud errors."""

import json

import httpx
import pytest

from hl7bridge.errors import FhirPostError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.mapper import patient_resource

from .test_fhir_mapper import patient_row


def make_client(handler) -> HapiClient:
    return HapiClient("http://fhir.test/fhir", transport=httpx.MockTransport(handler))


def test_upsert_sends_conditional_put():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"resourceType": "Patient", "id": "42"})

    with make_client(handler) as client:
        server_id, action = client.upsert(patient_resource(patient_row()))
    assert (server_id, action) == ("42", "created")
    assert seen["method"] == "PUT"
    assert "identifier=urn%3Ahl7bridge%3Ahospa%3Amrn%7CMA1001" in seen["url"]
    assert seen["body"]["resourceType"] == "Patient"


def test_upsert_200_reports_updated():
    def handler(_request):
        return httpx.Response(200, json={"resourceType": "Patient", "id": "42"})

    with make_client(handler) as client:
        assert client.upsert(patient_resource(patient_row()))[1] == "updated"


def test_upsert_412_multiple_matches_raises():
    def handler(_request):
        return httpx.Response(412, text="Multiple resources match this search")

    with make_client(handler) as client, pytest.raises(FhirPostError) as excinfo:
        client.upsert(patient_resource(patient_row()))
    assert excinfo.value.status_code == 412


def test_upsert_response_without_id_raises():
    def handler(_request):
        return httpx.Response(200, json={"resourceType": "Patient"})

    with make_client(handler) as client, pytest.raises(FhirPostError, match="no resource id"):
        client.upsert(patient_resource(patient_row()))


def test_search_total_uses_summary_count():
    def handler(request):
        assert request.url.params["_summary"] == "count"
        return httpx.Response(200, json={"resourceType": "Bundle", "total": 7})

    with make_client(handler) as client:
        assert client.search_total("Patient") == 7


def test_search_total_missing_total_raises():
    def handler(_request):
        return httpx.Response(200, json={"resourceType": "Bundle"})

    with make_client(handler) as client, pytest.raises(FhirPostError, match="no integer total"):
        client.search_total("Patient")
