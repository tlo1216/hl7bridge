"""Every repeat of PID-3, PID-5, PID-11, PID-13 and PID-14 is mapped, the
MRN repetition stays the key, and a post replaces names, addresses and
telephone numbers whole with the message's repeats (IG 1.0.0 datatype maps
for CX, XPN, XAD and XTN)."""

from __future__ import annotations

import copy

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.post import merge_resource, post_mapped
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.v2fhir import message_to_bundle
from hl7bridge.v2fhir.bundle import bundle_json

from ..fakefhir import FakeFhir

BASE = "http://fhir.test/fhir"
MRN_SYSTEM = "urn:hl7bridge:hospc:mrn"


@pytest.fixture(scope="module")
def registry() -> HospitalRegistry:
    return HospitalRegistry.load_dir(BUNDLED_PROFILES)


def _adt(pid3: str, *, event: str = "A08", **fields: str) -> str:
    """A synthetic HOSPC ADT; ``fields`` names PID fields by number
    (``f5``, ``f11``, ``f13``, ``f14``)."""
    pid = [""] * 15
    pid[0], pid[1], pid[3] = "PID", "1", pid3
    for key, value in fields.items():
        pid[int(key[1:])] = value
    msh = f"MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261007120000-0400||ADT^{event}|R{event}|T|2.5"
    return "\r".join([msh, f"EVN|{event}|20261007120000-0400", "|".join(pid)]) + "\r"


def _patient(text: str, registry: HospitalRegistry) -> dict:
    bundle = bundle_json(message_to_bundle(text, registry))
    return next(
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    )


def _requests(text: str, registry: HospitalRegistry) -> list[dict]:
    return [e["request"] for e in bundle_json(message_to_bundle(text, registry))["entry"]]


# -- PID-3 ------------------------------------------------------------------------


def test_every_identifier_is_kept_with_its_type_and_authority(registry):
    text = _adt(
        "SC9001^^^HOSPC^MR~ALT1^^^ALT&1.2.3.5&ISO^PI~U1^^^UU&0b1a2c3d-0000-4000-8000-0123456789ab&UUID^SS"
        "~W1^^^WEB&https://example.invalid/ids&URI~N1^^^SOMEWHERE^XX"
    )
    ids = _patient(text, registry)["identifier"]
    assert [i["value"] for i in ids] == ["SC9001", "ALT1", "U1", "W1", "N1"]
    assert ids[0] == {
        "type": {"coding": [{"system": _V2_0203, "code": "MR"}]},
        "system": MRN_SYSTEM,
        "value": "SC9001",
    }
    assert ids[1]["system"] == "urn:oid:1.2.3.5"
    assert ids[1]["type"]["coding"][0] == {"system": _V2_0203, "code": "PI"}
    assert ids[2]["system"] == "urn:uuid:0b1a2c3d-0000-4000-8000-0123456789ab"
    assert ids[3]["system"] == "https://example.invalid/ids" and "type" not in ids[3]
    assert "system" not in ids[4] and ids[4]["assigner"] == {"display": "SOMEWHERE"}


_V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"


def test_an_untyped_authority_with_no_oid_becomes_the_assigner(registry):
    ids = _patient(_adt("A1~B1^^^SITE"), registry)["identifier"]
    assert [i["value"] for i in ids] == ["A1", "B1"]
    assert ids[1] == {"value": "B1", "assigner": {"display": "SITE"}}


def test_empty_null_and_repeated_identifiers_are_skipped(registry):
    ids = _patient(_adt('SC9001^^^HOSPC^MR~~""~X1~X1'), registry)["identifier"]
    assert [i["value"] for i in ids] == ["SC9001", "X1"]


@pytest.mark.parametrize(
    "pid3",
    [
        "SC9001^^^HOSPC^MR",
        "SC9001^^^HOSPC^MR~ALT1^^^ALT&1.2.3.5&ISO^PI",
        "ALT1^^^ALT&1.2.3.5&ISO^PI~SC9001^^^HOSPC^MR",
        "ALT1^^^ALT&1.2.3.5&ISO^PI~SC9001^^^HOSPC^MR~Z9^^^X^SS",
    ],
)
def test_the_mrn_repetition_is_still_the_first_identifier_and_the_key(pid3, registry):
    patient = _patient(_adt(pid3), registry)
    assert patient["identifier"][0]["system"] == MRN_SYSTEM
    assert patient["identifier"][0]["value"] == "SC9001"
    assert _requests(_adt(pid3), registry)[0] == _requests(_adt("SC9001^^^HOSPC^MR"), registry)[0]


def test_untyped_repeats_keep_the_first_repetition_as_the_mrn(registry):
    patient = _patient(_adt("FIRST1~SECOND2"), registry)
    assert [i["value"] for i in patient["identifier"]] == ["FIRST1", "SECOND2"]
    assert patient["identifier"][0]["type"]["coding"][0]["code"] == "MR"
    assert "type" not in patient["identifier"][1]


# -- PID-5, PID-11, PID-13, PID-14 ------------------------------------------------


def test_every_name_repetition_with_prefix_suffix_and_use(registry):
    text = _adt(
        "SC9001^^^HOSPC^MR",
        f5="ZZZTEST^ALPHA^B^Jr^Dr^MD^L~ZZZOLD^ALPHA^^^^^M~ZZZNICK^AL^^^^^N~^^^^^^U",
    )
    names = _patient(text, registry)["name"]
    assert names == [
        {
            "use": "official",
            "family": "ZZZTEST",
            "given": ["ALPHA", "B"],
            "prefix": ["Dr"],
            "suffix": ["Jr", "MD"],
        },
        {"use": "maiden", "family": "ZZZOLD", "given": ["ALPHA"]},
        {"use": "nickname", "family": "ZZZNICK", "given": ["AL"]},
    ]


def test_a_name_type_the_ig_leaves_unmapped_gives_no_use(registry):
    name = _patient(_adt("SC9001", f5="ZZZ^A^^^^^B"), registry)["name"]
    assert name == [{"family": "ZZZ", "given": ["A"]}]


def test_every_address_repetition(registry):
    text = _adt(
        "SC9001",
        f11="1 TEST RD^UNIT 1^TESTVILLE^ZZ^00000^ZZ^H~2 TEST RD^^OTHERTOWN^ZZ^00000^ZZ^M^^COUNTY1",
    )
    addresses = _patient(text, registry)["address"]
    assert addresses == [
        {
            "use": "home",
            "line": ["1 TEST RD", "UNIT 1"],
            "city": "TESTVILLE",
            "state": "ZZ",
            "postalCode": "00000",
            "country": "ZZ",
        },
        {
            "line": ["2 TEST RD"],
            "city": "OTHERTOWN",
            "state": "ZZ",
            "district": "COUNTY1",
            "postalCode": "00000",
            "country": "ZZ",
        },
    ]


def test_every_telecom_repetition_and_pid14_is_work(registry):
    text = _adt(
        "SC9001",
        f13="^NET^Internet^zzz@example.invalid~5550100^PRN^PH~5550101^PRS^CP~5550102^^FX",
        f14="5550199^WPN^PH~5550198",
    )
    telecom = _patient(text, registry)["telecom"]
    assert telecom == [
        {"system": "email", "value": "zzz@example.invalid", "use": "home"},
        {"system": "phone", "value": "5550100", "use": "home"},
        {"system": "phone", "value": "5550101", "use": "mobile"},
        {"system": "fax", "value": "5550102", "use": "home"},
        {"system": "phone", "value": "5550199", "use": "work"},
        {"system": "phone", "value": "5550198", "use": "work"},
    ]


def test_repeated_values_are_written_once(registry):
    text = _adt(
        "SC9001",
        f5="ZZZ^A~ZZZ^A",
        f11="1 RD^^CITY~1 RD^^CITY",
        f13="5550100~5550100",
        f14="5550100",
    )
    patient = _patient(text, registry)
    assert len(patient["name"]) == len(patient["address"]) == 1
    assert [t["use"] for t in patient["telecom"]] == ["home", "work"]  # different use, kept


def test_one_repetition_messages_map_as_before(registry):
    text = _adt(
        "SC9001^^^HOSPC^MR",
        f5="ZZZTEST^ALPHA^B",
        f11="1 TEST RD^UNIT 1^TESTVILLE^ZZ^00000^ZZ",
        f13="5550100",
    )
    patient = _patient(text, registry)
    assert patient["name"] == [{"family": "ZZZTEST", "given": ["ALPHA", "B"]}]
    assert (
        patient["address"][0]["line"] == ["1 TEST RD", "UNIT 1"]
        and "use" not in patient["address"][0]
    )
    assert patient["telecom"] == [{"system": "phone", "value": "5550100", "use": "home"}]


# -- the key and the conditional URL are unchanged --------------------------------


def test_repeats_do_not_change_the_conditional_urls_or_full_urls(registry):
    plain = _adt("SC9001^^^HOSPC^MR", f5="ZZZ^A")
    rich = _adt(
        "ALT1^^^ALT&1.2.3.5&ISO^PI~SC9001^^^HOSPC^MR",
        f5="ZZZ^A~OLD^A^^^^^M",
        f11="1 RD^^CITY",
        f13="5550100",
    )
    a = bundle_json(message_to_bundle(plain, registry))
    b = bundle_json(message_to_bundle(rich, registry))
    assert [e["request"] for e in a["entry"]] == [e["request"] for e in b["entry"]]
    assert [e["fullUrl"] for e in a["entry"]] == [e["fullUrl"] for e in b["entry"]]
    query = "identifier=urn:hl7bridge:hospc:mrn|SC9001"
    assert b["entry"][0]["request"]["url"].endswith(query)


def test_committed_key_is_the_mrn_whatever_the_repeats(registry):
    server = FakeFhir()
    text = _adt("ALT1^^^ALT&1.2.3.5&ISO^PI~SC9001^^^HOSPC^MR", event="A01", f5="ZZZ^A~OLD^A^^^^^M")
    with HapiClient(BASE, transport=server.transport()) as client:
        done = post_mapped(client, message_to_bundle(text, registry))
    assert [c.key for c in done if c.kind == "Patient"] == [f"{MRN_SYSTEM}|SC9001"]
    assert server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")["identifier"][1]["value"] == "ALT1"
    assert server.by_key("Patient", "urn:oid:1.2.3.5|ALT1") is not None


# -- merge ------------------------------------------------------------------------


def test_merge_replaces_owned_lists_whole_and_keeps_server_identifiers():
    current = {
        "resourceType": "Patient",
        "identifier": [
            {"system": "urn:m", "value": "1"},
            {"system": "urn:server", "value": "S1"},
        ],
        "name": [{"family": "OLD"}, {"family": "STALE"}],
        "address": [{"city": "OLD"}],
        "telecom": [{"system": "phone", "value": "1"}],
    }
    new = {
        "resourceType": "Patient",
        "identifier": [
            {"system": "urn:m", "value": "1"},
            {"system": "urn:oid:1.2", "value": "A"},
            {"value": "B", "type": {"coding": [{"code": "PI"}]}},
            {"value": "B", "type": {"coding": [{"code": "SS"}]}},
        ],
        "name": [{"family": "NEW"}, {"family": "MAIDEN", "use": "maiden"}],
        "address": [{"city": "NEW1"}, {"city": "NEW2"}],
        "telecom": [{"system": "email", "value": "a@example.invalid"}],
    }
    before = copy.deepcopy(current)
    merged = merge_resource(current, new)
    assert current == before
    assert [i["value"] for i in merged["identifier"]] == ["1", "S1", "A", "B", "B"]
    assert [n["family"] for n in merged["name"]] == ["NEW", "MAIDEN"]
    assert [a["city"] for a in merged["address"]] == ["NEW1", "NEW2"]
    assert [t["value"] for t in merged["telecom"]] == ["a@example.invalid"]
    assert merge_resource(merged, new) == merged  # a second pass changes nothing


def test_merge_keeps_what_the_message_leaves_out():
    current = {"resourceType": "Patient", "address": [{"city": "OLD"}], "name": [{"family": "A"}]}
    merged = merge_resource(current, {"resourceType": "Patient", "name": [{"family": "B"}]})
    assert merged["address"] == [{"city": "OLD"}]
    assert merged["name"] == [{"family": "B"}]


def test_posting_the_same_message_twice_adds_no_duplicates(registry):
    server = FakeFhir()
    text = _adt(
        "SC9001^^^HOSPC^MR~ALT1^^^ALT&1.2.3.5&ISO^PI",
        event="A01",
        f5="ZZZ^A~OLD^A^^^^^M",
        f11="1 RD^^CITY~2 RD^^TOWN",
        f13="5550100~^NET^Internet^a@example.invalid",
    )
    with HapiClient(BASE, transport=server.transport()) as client:
        post_mapped(client, message_to_bundle(text, registry))
        first = copy.deepcopy(server.by_key("Patient", f"{MRN_SYSTEM}|SC9001"))
        post_mapped(client, message_to_bundle(text.replace("A01", "A08"), registry))
    second = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")
    assert len(server.all("Patient")) == 1
    for element in ("identifier", "name", "address", "telecom"):
        assert second[element] == first[element]
    assert len(second["identifier"]) == 2 and len(second["name"]) == 2


def test_an_a08_with_a_new_address_replaces_the_old_one(registry):
    server = FakeFhir()
    a01 = _adt("SC9001^^^HOSPC^MR", event="A01", f5="ZZZ^A~OLD^A^^^^^M", f11="1 RD^^CITY")
    a08 = _adt("SC9001^^^HOSPC^MR", f5="ZZZ^A", f11="2 ST^^TOWN", f13="5550100")
    with HapiClient(BASE, transport=server.transport()) as client:
        post_mapped(client, message_to_bundle(a01, registry))
        post_mapped(client, message_to_bundle(a08, registry))
    patient = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")
    assert [a["city"] for a in patient["address"]] == ["TOWN"]
    assert [n["family"] for n in patient["name"]] == ["ZZZ"]  # the dropped name is gone
    assert patient["telecom"][0]["value"] == "5550100"


def test_every_repeat_in_the_message_is_written(registry):
    server = FakeFhir()
    text = _adt(
        "SC9001^^^HOSPC^MR",
        event="A01",
        f5="ZZZ^A~OLD^A^^^^^M",
        f11="1 RD^^CITY~2 RD^^TOWN",
        f13="5550100~5550101",
        f14="5550102",
    )
    with HapiClient(BASE, transport=server.transport()) as client:
        post_mapped(client, message_to_bundle(text, registry))
    patient = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")
    assert [n["family"] for n in patient["name"]] == ["ZZZ", "OLD"]
    assert [a["city"] for a in patient["address"]] == ["CITY", "TOWN"]
    assert [t["value"] for t in patient["telecom"]] == ["5550100", "5550101", "5550102"]


def test_identifiers_the_server_holds_are_kept_by_a_post(registry):
    server = FakeFhir()
    a01 = _adt("SC9001^^^HOSPC^MR", event="A01", f5="ZZZ^A")
    a08 = _adt("SC9001^^^HOSPC^MR~ALT1^^^ALT&1.2.3.5&ISO^PI", f5="ZZZ^A")
    with HapiClient(BASE, transport=server.transport()) as client:
        post_mapped(client, message_to_bundle(a01, registry))
        stored = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")
        server._write(
            "Patient",
            stored["id"],
            {**stored, "identifier": [*stored["identifier"], {"system": "urn:s", "value": "S1"}]},
        )
        post_mapped(client, message_to_bundle(a08, registry))
    values = [i["value"] for i in server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")["identifier"]]
    assert values == ["SC9001", "S1", "ALT1"]


def test_a_thin_message_with_no_address_keeps_the_servers(registry):
    server = FakeFhir()
    a01 = _adt("SC9001^^^HOSPC^MR", event="A01", f5="ZZZ^A", f11="1 RD^^CITY", f13="5550100")
    thin = _adt("SC9001^^^HOSPC^MR", f5="ZZZ^A")  # no PID-11, no PID-13
    with HapiClient(BASE, transport=server.transport()) as client:
        post_mapped(client, message_to_bundle(a01, registry))
        post_mapped(client, message_to_bundle(thin, registry))
    patient = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")
    assert [a["city"] for a in patient["address"]] == ["CITY"]
    assert patient["telecom"][0]["value"] == "5550100"
