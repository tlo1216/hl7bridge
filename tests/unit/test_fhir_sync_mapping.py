"""fhir sync rebuilt from the stored messages, the post that ``to-fhir
--post`` and ``fhir sync`` share (merge and retire), the transaction check,
and the two paths writing identical content."""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
from decimal import Decimal

import httpx
import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import ConfigError, FhirPostError, MappingError
from hl7bridge.fhir import sync_to_fhir
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.post import (
    ConcurrentWriteError,
    MergeRefusedError,
    PostOrderError,
    check_run_order,
    merge_resource,
    post_mapped,
)
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


def test_sync_posts_a_merged_patients_own_messages_too(store, registry):
    store.execute("UPDATE patients SET merged_into_mrn = 'SB9999' WHERE mrn = 'SB1001'")
    store.commit()
    report, server = _sync(store, registry)
    assert not report.skipped
    assert len(server.by_identifier("Patient", "urn:hl7bridge:hospb:mrn", "SB1001")) == 1


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


FULL_PID = (
    "PID|1||SA9001^^^HOSPA^MR||ZZZSAMPLE^NINE||19800101|F|||1 TEST RD^^TESTVILLE^ZZ^00000||555-0101"
)
THIN_PID = "PID|1||SA9001^^^HOSPA^MR||ZZZSAMPLE^NINE"


def _with_pid(text: str, pid: str, control: str) -> str:
    lines = [pid if line.startswith("PID|") else line for line in text.split("\r")]
    out = "\r".join(lines)
    return out.replace("|SMPA0002|", f"|{control}|").replace("|SMPA0003|", f"|{control}|")


def _received_order_messages(superseded: bool) -> list[str]:
    """No ADT. A full-PID ORU arrives first, then a thin-PID ORM. With
    ``superseded`` the first ORU is a preliminary that a thin final replaces."""
    first = _with_pid(_sample("03_hospa_oru_r01_final.hl7"), FULL_PID, "SMPA0901")
    texts = [first.replace("|CR|F", "|CR|P") if superseded else first]
    texts.append(_with_pid(_sample("02_hospa_orm_o01.hl7"), THIN_PID, "SMPA0902"))
    if superseded:
        texts.append(_with_pid(_sample("03_hospa_oru_r01_final.hl7"), THIN_PID, "SMPA0903"))
    return texts


@pytest.mark.parametrize("superseded", [False, True])
def test_sync_and_direct_post_keep_the_same_patient(tmp_path, registry, superseded):
    texts = _received_order_messages(superseded)
    conn = connect(tmp_path / "order.db")
    pipeline = IngestPipeline(registry, conn)
    for text in texts:
        pipeline.handle_bytes(text.encode())
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == len(texts)
    synced = _sync(conn, registry)[1]
    conn.close()
    direct = FakeFhir()
    _post(direct, registry, *texts)
    key = "urn:hl7bridge:hospa:mrn|SA9001"
    want = direct.by_key("Patient", key)
    got = synced.by_key("Patient", key)
    for element in ("name", "gender", "birthDate", "address", "telecom"):
        assert got.get(element) == want.get(element), element
    assert got["address"][0]["city"] == "TESTVILLE"
    report = synced.by_key("DiagnosticReport", "urn:hl7bridge:hospa:accession|SACCA0001")
    assert report["status"] == "final"


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


def test_retiring_compares_the_parsed_accession_exactly(registry):
    # A key an accession like SACCA0002/B would have made: it starts with
    # this report's accession and a slash, but its accession part is not
    # this report's. It must never be retired.
    server = FakeFhir()
    _post(server, registry, STRUCTURED)
    report = server.by_key("DiagnosticReport", "urn:hl7bridge:hospa:accession|SACCA0002")
    for n, value in enumerate(["SACCA0002/B/sub/1/sub/2", "SACCA0002/B/x/1", "SACCA0002//sub/1"]):
        server._write(
            "Observation",
            f"60{n}",
            {
                "resourceType": "Observation",
                "status": "final",
                "identifier": [{"system": "urn:hl7bridge:hospa:observation", "value": value}],
            },
        )
        report["result"].append({"reference": f"Observation/60{n}"})
    server._write("DiagnosticReport", report["id"], report)
    _post(server, registry, _corrected(STRUCTURED))
    assert [server.data["Observation"][f"60{n}"]["status"] for n in range(3)] == ["final"] * 3
    # The result this report owns and dropped is still retired.
    finding = server.by_key("Observation", "urn:hl7bridge:hospa:observation|SACCA0002/FIND1/sub/1")
    assert finding["status"] == "entered-in-error"


@pytest.mark.parametrize("name", ["03_hospa_oru_r01_final.hl7", "14_hospa_oru_r01_structured.hl7"])
def test_an_accession_with_a_slash_is_refused_for_every_oru(registry, name):
    old = "SACCA0001" if name.startswith("03") else "SACCA0002"
    with pytest.raises(MappingError) as caught:
        message_to_bundle(_sample(name).replace(old, "SACCA0002/B"), registry)
    assert caught.value.field == "OBR-3"
    assert "SACCA" not in str(caught.value)


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


def test_a_post_older_than_the_report_on_the_server_is_refused(registry):
    from hl7bridge.fhir.post import StaleResultError

    server = FakeFhir()
    _post(server, registry, _corrected(STRUCTURED))
    before = len(server.transactions)
    with _client(server) as client, pytest.raises(StaleResultError):
        post_mapped(client, message_to_bundle(STRUCTURED, registry))
    assert len(server.transactions) == before
    report = server.by_key("DiagnosticReport", "urn:hl7bridge:hospa:accession|SACCA0002")
    assert report["status"] == "corrected"


def test_a_merge_create_that_another_writer_won_is_refused_not_overwritten(registry):
    from hl7bridge.fhir.post import ConcurrentWriteError

    server = FakeFhir()
    theirs = {
        "resourceType": "Encounter",
        "status": "planned",
        "identifier": [{"system": "urn:hl7bridge:hospc:visit", "value": "SV0001"}],
    }

    def interfere(body):
        server._write("Encounter", "theirs", theirs)
        server.before_commit = None
        return None

    server.before_commit = interfere
    with _client(server) as client, pytest.raises(ConcurrentWriteError):
        post_mapped(client, message_to_bundle(_sample("13_hospc_adt_a01_inpatient.hl7"), registry))
    assert server.data["Encounter"]["theirs"]["status"] == "planned"
    assert server.data["Encounter"]["theirs"]["meta"]["versionId"] == "1"


# -- ADT^A40 posted to the server ----------------------------------------------------

_HOSPC_MRN = "urn:hl7bridge:hospc:mrn"


def _a40(pid_mrn: str = "SC1001", gone: str = "SC1002") -> str:
    return (
        "\r".join(
            [
                f"MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261007083000||ADT^A40|TA40{pid_mrn}{gone}|T|2.5",
                "EVN|A40|20261007083000",
                f"PID|1||{pid_mrn}^^^HOSPC^MR||ZZZSAMPLE^THREE|||F",
                f"MRG|{gone}^^^HOSPC^MR",
            ]
        )
        + "\r"
    )


def _a08(mrn: str, family: str) -> str:
    return (
        "\r".join(
            [
                f"MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261007070000||ADT^A08|TA08{mrn}|T|2.5",
                f"PID|1||{mrn}^^^HOSPC^MR||{family}^ONE",
                "PV1|1|O",
            ]
        )
        + "\r"
    )


def test_a40_marks_the_merged_patient_inactive_and_keeps_its_demographics(registry):
    server = FakeFhir()
    _post(server, registry, _a08("SC1002", "OLDNAME"), _a08("SC1001", "KEEPER"), _a40())
    gone = server.by_key("Patient", f"{_HOSPC_MRN}|SC1002")
    survivor = server.by_key("Patient", f"{_HOSPC_MRN}|SC1001")
    assert gone["active"] is False
    assert gone["name"][0]["family"] == "OLDNAME"  # the merge read it first
    assert gone["link"] == [
        {"other": {"reference": f"Patient/{survivor['id']}"}, "type": "replaced-by"}
    ]
    assert survivor["name"][0]["family"] == "ZZZSAMPLE"
    assert "link" not in survivor


def test_a40_with_no_merged_patient_on_the_server_writes_a_bare_inactive_one(registry):
    server = FakeFhir()
    _post(server, registry, _a40())
    gone = server.by_key("Patient", f"{_HOSPC_MRN}|SC1002")
    assert gone["active"] is False and "name" not in gone
    assert gone["link"][0]["type"] == "replaced-by"


def test_a40_resend_and_a_late_a08_change_nothing_about_the_link(registry):
    server = FakeFhir()
    _post(server, registry, _a40())
    before = server.by_key("Patient", f"{_HOSPC_MRN}|SC1002")
    _post(server, registry, _a40())
    assert server.by_key("Patient", f"{_HOSPC_MRN}|SC1002")["link"] == before["link"]
    _post(server, registry, _a08("SC1002", "LATE"))
    after = server.by_key("Patient", f"{_HOSPC_MRN}|SC1002")
    assert after["active"] is False and after["link"] == before["link"]
    assert after["name"][0]["family"] == "LATE"
    assert len(server.all("Patient")) == 2


def test_sync_posts_the_stored_a40(store, registry):
    # The sample A40 (hospc) is in the store: SC1001 survives, SC1002 is merged away.
    report, server = _sync(store, registry)
    gone = server.by_key("Patient", f"{_HOSPC_MRN}|SC1002")
    survivor = server.by_key("Patient", f"{_HOSPC_MRN}|SC1001")
    assert gone["active"] is False
    assert gone["link"] == [
        {"other": {"reference": f"Patient/{survivor['id']}"}, "type": "replaced-by"}
    ]
    assert not any(kind == "ADT^A40" for kind, _, _ in report.skipped)


# -- ADT^A40: links kept, chains, contradictions, one resource twice -----------------

_GONE = f"{_HOSPC_MRN}|SC1002"


def _ref(server: FakeFhir, mrn: str) -> str:
    return f"Patient/{server.by_key('Patient', f'{_HOSPC_MRN}|{mrn}')['id']}"


def _links(server: FakeFhir, mrn: str) -> list[dict]:
    return server.by_key("Patient", f"{_HOSPC_MRN}|{mrn}").get("link", [])


def test_a40_keeps_the_links_the_merged_patient_already_has(registry):
    server = FakeFhir()
    _post(server, registry, _a08("SC1002", "OLDNAME"), _a08("SC1001", "KEEPER"))
    gone = server.by_key("Patient", _GONE)
    other = {"other": {"reference": "Patient/elsewhere"}, "type": "seealso"}
    gone["link"] = [other]
    server._write("Patient", gone["id"], gone)
    _post(server, registry, _a40())
    want = {"other": {"reference": _ref(server, "SC1001")}, "type": "replaced-by"}
    assert _links(server, "SC1002") == [other, want]
    _post(server, registry, _a40())  # a resend adds nothing
    assert _links(server, "SC1002") == [other, want]


def test_merge_resource_keeps_current_links_and_adds_only_new_ones():
    current = {
        "resourceType": "Patient",
        "link": [{"type": "refer", "other": {"reference": "P/1"}}],
    }
    new = {
        "resourceType": "Patient",
        "link": [
            {"type": "refer", "other": {"reference": "P/1"}},
            {"type": "seealso", "other": {"reference": "P/2"}},
        ],
    }
    merged = merge_resource(current, new)
    assert [x["type"] for x in merged["link"]] == ["refer", "seealso"]


def test_a40_chain_posts_every_edge_and_a_resend_is_quiet(registry):
    server2 = FakeFhir()  # _a40 takes (survivor, merged-away)
    _post(server2, registry, _a40("SC1002", "SC1001"), _a40("SC1003", "SC1002"))
    assert _links(server2, "SC1001")[0]["other"]["reference"] == _ref(server2, "SC1002")
    assert _links(server2, "SC1002")[0]["other"]["reference"] == _ref(server2, "SC1003")
    assert "link" not in server2.by_key("Patient", f"{_HOSPC_MRN}|SC1003")
    again = copy.deepcopy(server2.data)
    _post(server2, registry, _a40("SC1002", "SC1001"), _a40("SC1003", "SC1002"))
    assert {k: {i: r.get("link") for i, r in v.items()} for k, v in server2.data.items()} == {
        k: {i: r.get("link") for i, r in v.items()} for k, v in again.items()
    }


def test_sync_does_not_drop_the_first_a40_of_a_chain(tmp_path, registry):
    conn = connect(tmp_path / "c.db")
    pipeline = IngestPipeline(registry, conn)
    for text in (_a40("SC1002", "SC1001"), _a40("SC1003", "SC1002")):
        pipeline.handle_bytes(text.encode())
    report, server = _sync(conn, registry)
    again, _ = _sync(conn, registry, server)
    conn.close()
    assert report.skipped == [] and again.skipped == []
    assert _links(server, "SC1001")[0]["other"]["reference"] == _ref(server, "SC1002")
    assert _links(server, "SC1002")[0]["other"]["reference"] == _ref(server, "SC1003")
    assert server.by_key("Patient", _GONE)["active"] is False
    assert server.by_key("Patient", f"{_HOSPC_MRN}|SC1003").get("active") is None


def test_a_contradicting_a40_is_refused_and_writes_nothing(registry):
    server = FakeFhir()
    _post(server, registry, _a40("SC1002", "SC1001"))
    sent = len(server.transactions)
    with pytest.raises(MergeRefusedError, match="already replaced by another"):
        _post(server, registry, _a40("SC1003", "SC1001"))
    assert len(server.transactions) == sent
    assert _links(server, "SC1001")[0]["other"]["reference"] == _ref(server, "SC1002")


def test_a_reversed_a40_is_refused(registry):
    server = FakeFhir()
    _post(server, registry, _a40("SC1002", "SC1001"))
    sent = len(server.transactions)
    with pytest.raises(MergeRefusedError, match="surviving Patient is inactive or merged"):
        _post(server, registry, _a40("SC1001", "SC1002"))
    assert len(server.transactions) == sent
    assert "link" not in server.by_key("Patient", f"{_HOSPC_MRN}|SC1002")


def test_an_a40_into_an_inactive_survivor_is_refused(registry):
    server = FakeFhir()
    _post(server, registry, _a08("SC1001", "KEEPER"))
    survivor = server.by_key("Patient", f"{_HOSPC_MRN}|SC1001")
    survivor["active"] = False
    server._write("Patient", survivor["id"], survivor)
    with pytest.raises(MergeRefusedError):
        _post(server, registry, _a40("SC1001", "SC1002"))


def test_sync_skips_a_refused_a40_loudly_and_goes_on(tmp_path, registry):
    conn = connect(tmp_path / "r.db")
    pipeline = IngestPipeline(registry, conn)
    for text in (_a40("SC1002", "SC1001"), _a40("SC1003", "SC1001"), _a08("SC1004", "AFTER")):
        pipeline.handle_bytes(text.encode())
    report, server = _sync(conn, registry)
    conn.close()
    assert [(k, r[:34]) for k, _, r in report.skipped] == [
        ("ADT^A40", "the merged-away Patient is already")
    ]
    assert server.by_key("Patient", f"{_HOSPC_MRN}|SC1004")["name"][0]["family"] == "AFTER"


def test_two_entries_that_resolve_to_one_patient_are_refused(registry):
    server = FakeFhir()
    both = {
        "resourceType": "Patient",
        "identifier": [
            {"system": _HOSPC_MRN, "value": "SC1001"},
            {"system": _HOSPC_MRN, "value": "SC1002"},
        ],
    }
    server._write("Patient", "one", both)
    with pytest.raises(MergeRefusedError, match="resolve to one resource"):
        _post(server, registry, _a40())
    assert server.transactions == []
    assert "link" not in server.data["Patient"]["one"]


def test_the_fake_server_rejects_two_writes_to_one_id(registry):
    server = FakeFhir()
    server._write("Patient", "one", {"resourceType": "Patient", "identifier": []})
    entry = {
        "resource": {"resourceType": "Patient", "identifier": []},
        "fullUrl": "urn:uuid:a",
        "request": {"method": "PUT", "url": "Patient/one"},
    }
    body = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [entry, {**entry, "fullUrl": "urn:uuid:b"}],
    }
    with _client(server) as client, pytest.raises(FhirPostError, match="returned 400"):
        client.commit(body)
    assert server.data["Patient"]["one"]["meta"]["versionId"] == "1"


def test_a_merged_patient_created_by_another_writer_is_a_retryable_conflict(registry):
    server = FakeFhir()
    _post(server, registry, _a08("SC1001", "KEEPER"))

    def race(body):
        server.before_commit = None
        server._write(
            "Patient",
            "theirs",
            {"resourceType": "Patient", "identifier": [{"system": _HOSPC_MRN, "value": "SC1002"}]},
        )

    server.before_commit = race
    with pytest.raises(ConcurrentWriteError, match="send again"):
        _post(server, registry, _a40())
    # The resend completes the merge.
    _post(server, registry, _a40())
    assert _links(server, "SC1002")[0]["type"] == "replaced-by"
    assert len(server.all("Patient")) == 2


def test_the_batch_post_refuses_an_a40_for_a_match_only_profile(registry):
    mapped = dataclasses.replace(message_to_bundle(_a40(), registry), patient_policy="match_only")
    server = FakeFhir()
    with _client(server) as client, pytest.raises(ConfigError, match="match_only"):
        post_mapped(client, mapped)
    assert server.requests == []


def test_sync_and_to_fhir_agree_when_the_merged_patient_has_other_messages(store, registry):
    """The merged-away patient's later A08 is posted by both paths."""
    late = _a08("SC1002", "LATE")
    IngestPipeline(registry, store).handle_bytes(late.encode())
    _, synced = _sync(store, registry)
    posted = FakeFhir()
    texts = []
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        text = path.read_bytes().decode()
        try:
            message_to_bundle(text, registry)
        except MappingError:
            continue
        texts.append(text)
    _post(posted, registry, *texts, late)
    assert _normalized(posted) == _normalized(synced)
    gone = synced.by_key("Patient", _GONE)
    assert gone["name"][0]["family"] == "LATE" and gone["active"] is False
