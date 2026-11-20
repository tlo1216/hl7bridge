"""fhir sync rebuilt from the stored messages, the post that ``to-fhir
--post`` and ``fhir sync`` share (merge and retire), the transaction check,
and the two paths writing identical content."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import httpx
import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import FhirPostError
from hl7bridge.fhir import sync_to_fhir
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.post import PostOrderError, check_run_order, merge_resource, post_mapped
from hl7bridge.ingest import IngestPipeline
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.store import connect
from hl7bridge.v2fhir import message_to_bundle
from hl7bridge.v2fhir.jsontext import dumps, loads

from ..fakefhir import FakeFhir

BASE = "http://fhir.test/fhir"


@pytest.fixture
def registry() -> HospitalRegistry:
    return HospitalRegistry.load_dir(BUNDLED_PROFILES)


@pytest.fixture
def store(tmp_path, registry):
    conn = connect(tmp_path / "s.db")
    pipeline = IngestPipeline(registry, conn)
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        pipeline.handle_bytes(path.read_bytes())
    yield conn
    conn.close()


def _client(server: FakeFhir) -> HapiClient:
    return HapiClient(BASE, transport=server.transport())


def _sync(conn, registry, server: FakeFhir | None = None):
    server = server or FakeFhir()
    with _client(server) as client:
        report = sync_to_fhir(conn, registry, client)
    return report, server


def _post(server: FakeFhir, registry, *texts: str) -> None:
    with _client(server) as client:
        for text in texts:
            post_mapped(client, message_to_bundle(text, registry))


def _sample(name: str) -> str:
    return (SAMPLES_DIR / name).read_bytes().decode()


def _set_raw(conn, old: str, new: str) -> None:
    rows = conn.execute("SELECT id, raw FROM messages WHERE raw LIKE ?", (f"%{old}%",)).fetchall()
    assert rows, old
    for row in rows:
        conn.execute(
            "UPDATE messages SET raw = ? WHERE id = ?", (row["raw"].replace(old, new), row["id"])
        )
    conn.commit()


# -- sync from the stored messages -----------------------------------------------------


def test_sync_posts_the_stored_visit_as_an_encounter(store, registry):
    report, server = _sync(store, registry)
    assert report.created["Encounter"] == 1
    assert not report.skipped
    enc = server.by_key("Encounter", "urn:hl7bridge:hospc:visit|SV0001")
    # The A08 discharge came after the A01: the visit is finished, and the
    # admit time the A01 carried is still there.
    assert enc["status"] == "finished"
    assert enc["class"]["code"] == "IMP"
    assert enc["period"] == {"start": "2026-10-07T15:55:00Z", "end": "2026-10-09T13:30:00Z"}
    assert enc["subject"]["reference"].startswith("Patient/")


def test_sync_leaves_out_a_merged_patient(store, registry):
    store.execute("UPDATE patients SET merged_into_mrn = 'SB9999' WHERE mrn = 'SB1001'")
    store.commit()
    report, server = _sync(store, registry)
    assert report.merged_away == 3  # its A08, ORM and ORU
    assert not report.skipped
    assert server.by_identifier("Patient", "urn:hl7bridge:hospb:mrn", "SB1001") == []
    assert server.all("Patient")


def test_sync_skips_a_message_whose_sex_code_is_not_in_the_table(store, registry, caplog):
    canary = "QZ"
    _set_raw(store, "19650412|M|", f"19650412|{canary}|")
    with caplog.at_level(logging.DEBUG):
        report, server = _sync(store, registry)
    reasons = {(kind, reason) for kind, _, reason in report.skipped}
    reason = "PID-8: code not in table 0001 (administrative sex); refusing to guess"
    assert ("ADT^A01", reason) in reasons and ("ADT^A08", reason) in reasons
    assert server.by_identifier("Encounter", "urn:hl7bridge:hospc:visit", "SV0001") == []
    assert canary not in caplog.text


def test_sync_skips_a_message_whose_class_is_not_in_the_table(store, registry):
    _set_raw(store, "PV1|1|I|", "PV1|1|Z|")
    report, _ = _sync(store, registry)
    assert any(
        reason == "PV1-2: code not in table 0004 (patient class); refusing to guess"
        for _, _, reason in report.skipped
    )


def test_a_second_sync_creates_nothing(store, registry):
    first, server = _sync(store, registry)
    second, _ = _sync(store, registry, server)
    assert sum(first.created.values()) > 0
    assert sum(second.created.values()) == 0
    assert second.skipped == []


def _normalized(server: FakeFhir) -> dict[tuple[str, str], dict]:
    """Every resource by (type, key), without server ids and versions, and
    with each literal reference replaced by the key of what it points to."""
    keys = {}
    for kind, items in server.data.items():
        for server_id, resource in items.items():
            ident = resource["identifier"][0]
            keys[f"{kind}/{server_id}"] = f"{kind} {ident['system']}|{ident['value']}"

    def clean(value):
        if isinstance(value, dict):
            return {
                k: (keys.get(v, v) if k == "reference" else clean(v))
                for k, v in value.items()
                if k not in ("id", "meta")
            }
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    found = {}
    for kind, items in server.data.items():
        if kind == "ImagingStudy":
            continue
        for resource in items.values():
            ident = resource["identifier"][0]
            found[(kind, f"{ident['system']}|{ident['value']}")] = clean(resource)
    return found


def test_sync_and_to_fhir_write_identical_content(store, registry):
    """Posting the messages with to-fhir and syncing the store that took the
    same messages leave the same resources, element for element."""
    _, synced = _sync(store, registry)
    posted = FakeFhir()
    texts = []
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        text = path.read_bytes().decode()
        try:
            mapped = message_to_bundle(text, registry)
        except Exception:  # noqa: BLE001  the refused samples are covered elsewhere
            continue
        texts.append(text)
        assert mapped
    _post(posted, registry, *texts)
    assert _normalized(posted) == _normalized(synced)
    kinds = {k for k, _ in _normalized(synced)}
    assert kinds == {"Patient", "Encounter", "ServiceRequest", "Observation", "DiagnosticReport"}


def test_sync_after_to_fhir_changes_nothing(store, registry):
    server = FakeFhir()
    _post(server, registry, _sample("14_hospa_oru_r01_structured.hl7"))
    before = _normalized(server)
    _sync(store, registry, server)
    after = _normalized(server)
    for key, resource in before.items():
        assert after[key] == resource, key


# -- the merge ---------------------------------------------------------------------


def test_a_thin_oru_never_erases_the_patient(registry):
    server = FakeFhir()
    a04 = _sample("01_hospa_adt_a04.hl7").replace(
        "|19800101|F", "|19800101|F|||1 TEST RD^^TESTVILLE^ZZ^00000||555-0101"
    )
    thin = _sample("03_hospa_oru_r01_final.hl7").replace("ZZZSAMPLE^ONE||19800101|F", "")
    _post(server, registry, a04, thin)
    patient = server.by_key("Patient", "urn:hl7bridge:hospa:mrn|SA1001")
    assert patient["address"][0]["city"] == "TESTVILLE"
    assert patient["telecom"][0]["value"] == "555-0101"
    assert patient["name"][0]["family"] == "ZZZSAMPLE"
    assert patient["meta"]["versionId"] == "1"  # the ORU did not write it


def test_an_oru_creates_a_missing_patient(registry):
    server = FakeFhir()
    _post(server, registry, _sample("03_hospa_oru_r01_final.hl7"))
    assert server.by_key("Patient", "urn:hl7bridge:hospa:mrn|SA1001")["gender"] == "female"


def test_an_a08_keeps_what_it_leaves_out(registry):
    server = FakeFhir()
    a01 = _sample("13_hospc_adt_a01_inpatient.hl7")
    # An A08 with no address, no phone, no admit time and no discharge.
    a08 = (
        "MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261008100000-0400||ADT^A08|C8|T|2.5\r"
        "EVN|A08|20261008100000-0400\r"
        "PID|1||SC1003^^^HOSPC^MR||ZZZSAMPLE^FOUR^Q||19650412|M\r"
        "PV1|1|I|||||||||||||||||SV0001\r"
    )
    _post(server, registry, a01, a08)
    before = message_to_bundle(a01, registry)
    patient = server.by_key("Patient", "urn:hl7bridge:hospc:mrn|SC1003")
    first = dumps(before.bundle.entry[0].resource.model_dump(mode="json", exclude_none=True))
    for element in ("address", "telecom"):
        assert patient.get(element) == loads(first).get(element), element
    enc = server.by_key("Encounter", "urn:hl7bridge:hospc:visit|SV0001")
    assert enc["period"]["start"] == "2026-10-07T15:55:00Z"
    assert enc["status"] == "in-progress"
    assert patient["meta"]["versionId"] == "2"


def test_a_discharge_stays_finished_after_a_later_a08(registry):
    server = FakeFhir()
    a08 = (
        "MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261010100000-0400||ADT^A08|C9|T|2.5\r"
        "EVN|A08|20261010100000-0400\r"
        "PID|1||SC1003^^^HOSPC^MR||ZZZSAMPLE^FOUR^Q||19650412|M\r"
        "PV1|1|I|||||||||||||||||SV0001\r"
    )
    _post(
        server,
        registry,
        _sample("13_hospc_adt_a01_inpatient.hl7"),
        _sample("15_hospc_adt_a08_discharge.hl7"),
        a08,
    )
    enc = server.by_key("Encounter", "urn:hl7bridge:hospc:visit|SV0001")
    assert enc["status"] == "finished"
    assert enc["period"]["end"] == "2026-10-09T13:30:00Z"


def test_a_cancel_keeps_the_order_time(registry):
    server = FakeFhir()
    nw = _sample("02_hospa_orm_o01.hl7")
    ca = nw.replace(
        "ORC|NW|PLSMPA0002|||||||20261007081500", "ORC|CA|PLSMPA0002|||||||20261008090000"
    )
    ca = ca.replace("SMPA0002|T", "SMPA0102|T")
    assert "authoredOn" not in json.loads(
        dumps(message_to_bundle(ca, registry).bundle.entry[1].resource.model_dump(mode="json"))
    )
    _post(server, registry, nw, ca)
    order = server.by_key("ServiceRequest", "urn:hl7bridge:hospa:accession|SACCA0001")
    assert order["status"] == "revoked"
    assert order["authoredOn"] == "2026-10-07T15:15:00Z"  # the NW's ORC-9, Los Angeles


def test_merge_keeps_elements_the_mapper_does_not_own():
    current = {
        "resourceType": "Patient",
        "id": "7",
        "meta": {"versionId": "3", "tag": [{"code": "keep"}]},
        "identifier": [{"system": "urn:other", "value": "X"}, {"system": "urn:m", "value": "1"}],
        "name": [{"family": "OLD"}],
        "address": [{"city": "KEEP"}],
        "extension": [{"url": "urn:x", "valueString": "keep"}],
    }
    new = {
        "resourceType": "Patient",
        "identifier": [{"system": "urn:m", "value": "1"}],
        "name": [{"family": "NEW"}],
    }
    merged = merge_resource(current, new)
    assert merged["name"] == [{"family": "NEW"}]
    assert merged["address"] == [{"city": "KEEP"}]
    assert merged["extension"] == current["extension"]
    assert merged["meta"]["tag"] == [{"code": "keep"}]
    assert len(merged["identifier"]) == 2
    assert current["name"] == [{"family": "OLD"}]  # not changed in place


def test_a_change_between_read_and_write_fails_the_post(registry):
    server = FakeFhir()
    _post(server, registry, _sample("13_hospc_adt_a01_inpatient.hl7"))

    def someone_else_writes(body):
        patient = server.by_key("Patient", "urn:hl7bridge:hospc:mrn|SC1003")
        server._write("Patient", patient["id"], {**patient, "gender": "other"})

    server.before_commit = someone_else_writes
    with pytest.raises(FhirPostError, match="412"):
        _post(server, registry, _sample("15_hospc_adt_a08_discharge.hl7"))
    assert server.by_key("Patient", "urn:hl7bridge:hospc:mrn|SC1003")["gender"] == "other"


def test_two_resources_with_one_key_are_refused(registry):
    server = FakeFhir()
    _post(server, registry, _sample("01_hospa_adt_a04.hl7"))
    patient = server.by_key("Patient", "urn:hl7bridge:hospa:mrn|SA1001")
    server._write("Patient", "999", {**patient})
    with pytest.raises(FhirPostError, match="more than one Patient has this identifier"):
        _post(server, registry, _sample("01_hospa_adt_a04.hl7"))


# -- retiring results a correction dropped ---------------------------------------------


STRUCTURED = _sample("14_hospa_oru_r01_structured.hl7")


def _corrected(text: str) -> str:
    """The structured report corrected: the coded finding (and its note)
    dropped, the measurement kept, issued an hour later."""
    lines = [line for line in text.split("\r") if not line.startswith(("OBX|2|", "NTE|"))]
    return (
        "\r".join(lines)
        .replace("|US|F", "|US|C")
        .replace("20261007120000||US", "20261007130000||US")
        .replace("SMPA0005", "SMPA0105")
    )


def test_a_correction_retires_the_results_it_no_longer_carries(registry):
    server = FakeFhir()
    _post(server, registry, STRUCTURED)
    with _client(server) as client:
        done = post_mapped(client, message_to_bundle(_corrected(STRUCTURED), registry))
    assert [c.kind for c in done if c.retired] == ["Observation"]
    finding = server.by_key("Observation", "urn:hl7bridge:hospa:observation|SACCA0002/FIND1/sub/1")
    measure = server.by_key("Observation", "urn:hl7bridge:hospa:observation|SACCA0002/MEAS1/sub/1")
    assert finding["status"] == "entered-in-error"
    assert measure["status"] == "final"
    report = server.by_key("DiagnosticReport", "urn:hl7bridge:hospa:accession|SACCA0002")
    assert report["status"] == "corrected"
    assert report["result"] == [{"reference": f"Observation/{measure['id']}"}]
    # A resend of the correction retires nothing more.
    with _client(server) as client:
        again = post_mapped(client, message_to_bundle(_corrected(STRUCTURED), registry))
    assert not any(c.retired for c in again)


def test_retiring_never_touches_another_reports_or_systems_results(registry):
    server = FakeFhir()
    _post(server, registry, STRUCTURED)
    report = server.by_key("DiagnosticReport", "urn:hl7bridge:hospa:accession|SACCA0002")
    for n, ident in enumerate(
        [
            {"system": "urn:someone-else", "value": "SACCA0002/X/sub/1"},
            {"system": "urn:hl7bridge:hospa:observation", "value": "SACCA0009/X/sub/1"},
        ]
    ):
        server._write(
            "Observation",
            f"50{n}",
            {"resourceType": "Observation", "status": "final", "identifier": [ident]},
        )
        report["result"].append({"reference": f"Observation/50{n}"})
    server._write("DiagnosticReport", report["id"], report)
    _post(server, registry, _corrected(STRUCTURED))
    assert server.data["Observation"]["500"]["status"] == "final"
    assert server.data["Observation"]["501"]["status"] == "final"


def test_run_order_refuses_a_preliminary_after_a_final(registry):
    final = message_to_bundle(STRUCTURED, registry)
    prelim = message_to_bundle(
        STRUCTURED.replace("|US|F", "|US|P").replace("SMPA0005", "SMPA0205"), registry
    )
    check_run_order([("p", prelim), ("f", final)])
    with pytest.raises(PostOrderError, match="p holds an older version of the same report as f"):
        check_run_order([("f", final), ("p", prelim)])
    earlier = message_to_bundle(
        STRUCTURED.replace("20261007120000||US|F", "20261007113000||US|C"), registry
    )
    with pytest.raises(PostOrderError):  # a correction issued before the final
        check_run_order([("f", final), ("c", earlier)])


# -- decimals ---------------------------------------------------------------------


def test_posted_json_keeps_decimal_digits(registry):
    server = FakeFhir()
    text = STRUCTURED.replace("|12.5|", "|12.50|")
    _post(server, registry, text)
    sent = server.transactions[0]
    obs = next(e for e in sent["entry"] if e["resource"]["resourceType"] == "Observation")
    assert obs["resource"]["valueQuantity"]["value"] == Decimal("12.50")
    assert str(obs["resource"]["valueQuantity"]["value"]) == "12.50"
    seen: list[bytes] = []

    def capture(request):
        if request.method == "POST":
            seen.append(request.content)
        return server(request)

    with HapiClient(BASE, transport=httpx.MockTransport(capture)) as client:
        post_mapped(client, message_to_bundle(text, registry))
    assert b'"value": 12.50,' in seen[0]
    assert b"12.5," not in seen[0]


# -- the transaction check -----------------------------------------------------------


def _bundle(registry, name="14_hospa_oru_r01_structured.hl7"):
    return message_to_bundle((SAMPLES_DIR / name).read_bytes().decode(), registry).bundle


N_ENTRIES = 4  # Patient, two Observations, DiagnosticReport


def _answer(n: int, status: str = "201 Created", **overrides) -> dict:
    body = {
        "resourceType": "Bundle",
        "type": "transaction-response",
        "entry": [{"response": {"status": status}} for _ in range(n)],
    }
    body.update(overrides)
    return body


def _commit(registry, handler):
    with HapiClient(BASE, transport=httpx.MockTransport(handler)) as client:
        return client.transaction(_bundle(registry))


def test_a_valid_transaction_response_is_accepted(registry):
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_answer(N_ENTRIES))

    got = _commit(registry, handler)
    assert seen["method"] == "POST" and seen["path"] == "/fhir"
    assert seen["body"]["type"] == "transaction"
    assert got == [
        ("Patient", 201),
        ("Observation", 201),
        ("Observation", 201),
        ("DiagnosticReport", 201),
    ]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_answer(N_ENTRIES, type="batch-response"), "not a transaction-response Bundle"),
        # Only the resource type is wrong; every other check would pass.
        (_answer(N_ENTRIES, resourceType="Parameters"), "not a transaction-response Bundle"),
        ({"resourceType": "OperationOutcome"}, "not a transaction-response Bundle"),
        (_answer(3), "the answer has 3 entries for 4 requests"),
        (_answer(5), "the answer has 5 entries for 4 requests"),
        (_answer(N_ENTRIES, entry=None), "the answer has 0 entries for 4 requests"),
        (_answer(N_ENTRIES, status="400 Bad Request"), "entry 1 was not committed"),
        (_answer(N_ENTRIES, status="2010 Created"), "entry 1 was not committed"),
        (_answer(N_ENTRIES, status="Created"), "entry 1 was not committed"),
        (_answer(N_ENTRIES, status="204 No Content"), "entry 1 was not committed"),
    ],
)
def test_anything_short_of_a_full_commit_is_an_error(registry, answer, reason):
    with pytest.raises(FhirPostError, match=reason):
        _commit(registry, lambda _r: httpx.Response(200, json=answer))


def test_one_failed_entry_among_good_ones_is_an_error(registry):
    answer = _answer(N_ENTRIES)
    answer["entry"][2]["response"]["status"] = "412 Precondition Failed"
    with pytest.raises(FhirPostError, match="entry 3 was not committed"):
        _commit(registry, lambda _r: httpx.Response(200, json=answer))


def test_a_non_200_answer_is_an_error_without_the_body(registry):
    body = {
        "resourceType": "OperationOutcome",
        "issue": [{"code": "invalid", "diagnostics": "SA1001"}],
    }
    with pytest.raises(FhirPostError) as info:
        _commit(registry, lambda _r: httpx.Response(400, json=body))
    assert "SA1001" not in str(info.value)
    assert "invalid" in str(info.value)


def test_an_answer_that_is_not_json_is_an_error(registry):
    with pytest.raises(FhirPostError, match="not JSON"):
        _commit(registry, lambda _r: httpx.Response(200, text="<html>"))
