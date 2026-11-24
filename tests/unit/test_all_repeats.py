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


# -- PID-3.4 escapes ----------------------------------------------------------------
#
# python-hl7 splits a field on the message's own separators and only then
# unescapes each leaf, so a separator a sender escaped inside the namespace
# (HD.1) is part of it. Splitting the unescaped value again cut it short.

_ESCAPED = [
    ("ST\\T\\MARY", "ST&MARY"),
    ("ST\\X26\\MARY", "ST&MARY"),
    ("ST\\S\\MARY", "ST^MARY"),
    ("ST\\R\\MARY", "ST~MARY"),
    ("ST\\E\\MARY", "ST\\MARY"),
    ("ST\\F\\MARY", "ST|MARY"),
    ("A\\T\\B\\S\\C\\R\\D\\E\\E\\F\\F", "A&B^C~D\\E|F"),
    ("ST\\E\\T\\E\\MARY", "ST\\T\\MARY"),  # decoded once, never twice
]


def _authority(text: str, registry: HospitalRegistry) -> str:
    return message_to_bundle(text, registry).mrn_authority


@pytest.mark.parametrize(("sent", "authority"), _ESCAPED)
def test_an_escaped_separator_stays_in_the_mrn_authority(sent, authority, registry):
    assert _authority(_adt(f"SC9001^^^{sent}^MR"), registry) == authority
    hd = f"SC9001^^^{sent}&2.16.840.1.113883.3.999&ISO^MR"
    assert _authority(_adt(hd), registry) == authority


def test_the_mrn_authority_is_the_namespace_of_a_full_hd(registry):
    assert _authority(_adt("SC9001^^^HOSPC&1.2.3.4&ISO^MR"), registry) == "HOSPC"
    # Only HD.1 is the authority here; a universal id alone is not read for
    # the MRN system. Unchanged by the escape fix.
    assert _authority(_adt("SC9001^^^&1.2.3.4&ISO^MR"), registry) == ""
    assert _authority(_adt("SC9001^^^^MR"), registry) == ""


def test_escaped_separators_in_other_authorities_are_kept_whole(registry):
    text = _adt(
        "SC9001^^^HOSPC^MR~A1^^^ALT\\T\\X&1.2.3.5&ISO^PI~B1^^^OTHER\\T\\SITE\\S\\2^XX"
        "~C1^^^\\T\\&&ISO^SS"
    )
    ids = _patient(text, registry)["identifier"]
    assert [i["value"] for i in ids] == ["SC9001", "A1", "B1", "C1"]
    assert ids[1]["system"] == "urn:oid:1.2.3.5" and "assigner" not in ids[1]
    assert ids[2]["assigner"] == {"display": "OTHER&SITE^2"} and "system" not in ids[2]
    assert ids[3]["assigner"] == {"display": "&"} and "system" not in ids[3]


def test_repeated_pid3_takes_the_authority_of_the_mr_repetition(registry):
    text = _adt(
        "X1^^^OTHER\\T\\SITE^PI~SC9001^^^ST\\T\\MARY&1.2.3.4&ISO^MR~Y1^^^ST^SS"
        "~Z1^^^ST\\T\\JOHN&1.2.3.6&ISO^MR"
    )
    assert _authority(text, registry) == "ST&MARY"
    ids = _patient(text, registry)["identifier"]
    assert [i["value"] for i in ids] == ["SC9001", "X1", "Y1", "Z1"]
    assert ids[1]["assigner"] == {"display": "OTHER&SITE"}
    assert ids[2]["assigner"] == {"display": "ST"}
    assert ids[3]["system"] == "urn:oid:1.2.3.6"


# Field, component, repetition, escape and subcomponent separators that are
# not the defaults: none of | ^ ~ \ & is a separator in the message any more.
_OTHER = str.maketrans({"|": "!", "^": "$", "~": "*", "\\": "@", "&": "%"})


@pytest.mark.parametrize(
    ("pid3", "authority"),
    [
        ("SC9001$$$A&B^C~D\\E|F$MR", "A&B^C~D\\E|F"),  # all plain data here
        ("SC9001$$$ST&MARY%1.2.3.4%ISO$MR", "ST&MARY"),
        ("SC9001$$$ST@T@MARY%1.2.3.4%ISO$MR", "ST%MARY"),
        ("X1$$$ST$PI*SC9001$$$ST&JOHN%1.2.3.4%ISO$MR", "ST&JOHN"),
    ],
)
def test_the_message_own_separators_split_pid3(pid3, authority, registry):
    text = _adt("PLACEHOLDER").translate(_OTHER).replace("PLACEHOLDER", pid3)
    assert text.startswith("MSH!$*@%!")
    assert _authority(text, registry) == authority
    assert _patient(text, registry)["identifier"][0]["value"] == "SC9001"


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
        {"system": "email", "value": "zzz@example.invalid"},
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


# -- review fixes: key order, authorities, nulls, XTN values, telecom use ----------


def _seed_a01(server: FakeFhir, registry, **fields: str) -> None:
    text = _adt("SC9001^^^HOSPC^MR", event="A01", **fields)
    with HapiClient(BASE, transport=server.transport()) as client:
        post_mapped(client, message_to_bundle(text, registry))


def _post_text(server: FakeFhir, registry, text: str):
    with HapiClient(BASE, transport=server.transport()) as client:
        return post_mapped(client, message_to_bundle(text, registry))


@pytest.mark.parametrize(
    "first",
    [{"value": "S-NO-SYSTEM"}, {"system": "urn:other:ssn", "value": "000-00-0000"}],
)
def test_committed_key_is_the_mrn_whatever_order_the_server_lists_identifiers(first, registry):
    server = FakeFhir()
    _seed_a01(server, registry, f5="ZZZ^A")
    held = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")
    held["identifier"] = [first, *held["identifier"]]
    server._write("Patient", held["id"], held)
    done = _post_text(server, registry, _adt("SC9001^^^HOSPC^MR", f5="NEWNAME^A"))
    assert [c.key for c in done if c.kind == "Patient"] == [f"{MRN_SYSTEM}|SC9001"]
    assert server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")["name"][0]["family"] == "NEWNAME"


def test_a_retired_result_key_ignores_a_systemless_identifier_listed_first(registry):
    from hl7bridge.fhir.post import _retired_key

    ours = {"system": "urn:obs", "value": "ACC/CODE/sub/1"}
    entry = {"resource": {"identifier": [{"value": "X"}, ours]}}
    assert _retired_key(entry["resource"], "urn:obs") == "urn:obs|ACC/CODE/sub/1"


def test_systemless_identifiers_from_different_authorities_are_both_kept(registry):
    text = _adt("M1^^^HOSPC^MR~123^^^SITE_A^PI~123^^^SITE_B^PI")
    ids = _patient(text, registry)["identifier"]
    assert [(i["value"], i["assigner"]["display"]) for i in ids[1:]] == [
        ("123", "SITE_A"),
        ("123", "SITE_B"),
    ]


def test_a_post_keeps_a_second_systemless_identifier_from_another_authority(registry):
    server = FakeFhir()
    _post_text(server, registry, _adt("M1^^^HOSPC^MR~123^^^SITE_A^PI", event="A01"))
    _post_text(server, registry, _adt("M1^^^HOSPC^MR~123^^^SITE_A^PI~123^^^SITE_B^PI"))
    ids = server.by_key("Patient", f"{MRN_SYSTEM}|M1")["identifier"]
    assert [i["assigner"]["display"] for i in ids[1:]] == ["SITE_A", "SITE_B"]


def test_merge_resource_keeps_identifiers_that_differ_only_by_authority():
    a = {"value": "123", "assigner": {"display": "A"}}
    b = {"value": "123", "assigner": {"display": "B"}}
    merged = merge_resource(
        {"resourceType": "Patient", "identifier": [a]},
        {"resourceType": "Patient", "identifier": [a, b]},
    )
    assert merged["identifier"] == [a, b]


@pytest.mark.parametrize(
    ("field", "element"), [("f5", "name"), ("f11", "address"), ("f13", "telecom")]
)
def test_an_explicit_hl7_null_clears_the_field_on_the_server(field, element, registry):
    server = FakeFhir()
    _seed_a01(server, registry, f5="ZZZ^A", f11="1 RD^^CITY", f13="5550100")
    _post_text(server, registry, _adt("SC9001^^^HOSPC^MR", **{field: '""'}))
    patient = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")
    assert element not in patient or patient[element] == []
    for other in {"name", "address", "telecom"} - {element}:
        assert patient[other]  # the fields the message left absent are kept


def test_an_explicit_null_in_pid14_alone_clears_the_telephone_list(registry):
    server = FakeFhir()
    _seed_a01(server, registry, f13="5550100")
    _post_text(server, registry, _adt("SC9001^^^HOSPC^MR", f14='""'))
    assert not server.by_key("Patient", f"{MRN_SYSTEM}|SC9001").get("telecom")


def test_a_null_beside_a_value_does_not_clear(registry):
    server = FakeFhir()
    _seed_a01(server, registry, f13="5550100")
    _post_text(server, registry, _adt("SC9001^^^HOSPC^MR", f13='5550111~""'))
    telecom = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")["telecom"]
    assert [t["value"] for t in telecom] == ["5550111"]


def test_an_absent_field_still_keeps_the_server_value(registry):
    server = FakeFhir()
    _seed_a01(server, registry, f5="ZZZ^A", f11="1 RD^^CITY", f13="5550100")
    _post_text(server, registry, _adt("SC9001^^^HOSPC^MR"))
    patient = server.by_key("Patient", f"{MRN_SYSTEM}|SC9001")
    assert patient["name"] and patient["address"] and patient["telecom"]


def test_the_null_is_not_in_the_bundle_resource(registry):
    patient = _patient(_adt("SC9001^^^HOSPC^MR", f5='""'), registry)
    assert "name" not in patient


@pytest.mark.parametrize(
    ("xtn", "expected"),
    [
        ("^PRN^PH^^1^312^5550199", {"system": "phone", "value": "+1 312 5550199", "use": "home"}),
        ("^PRN^PH^^^312^5550199", {"system": "phone", "value": "312 5550199", "use": "home"}),
        ("^PRN^PH^^^^5550199", {"system": "phone", "value": "5550199", "use": "home"}),
        ("111^PRN^PH^^1^312^5550199", {"system": "phone", "value": "111", "use": "home"}),
        (
            "111^PRN^PH^^1^312^5550199^^^^^(312) 555-0199",
            {"system": "phone", "value": "(312) 555-0199", "use": "home"},
        ),
        (
            "^PRN^PH^^^^^^^^^(312) 555-0199",
            {"system": "phone", "value": "(312) 555-0199", "use": "home"},
        ),
    ],
)
def test_xtn_value_rules(xtn, expected, registry):
    assert _patient(_adt("SC9001", f13=xtn), registry)["telecom"] == [expected]


def test_a_built_number_telecom_is_not_dropped_beside_an_email(registry):
    text = _adt("SC9001", f13="^PRN^PH^^1^312^5550199~^NET^Internet^a@example.invalid")
    values = [t["value"] for t in _patient(text, registry)["telecom"]]
    assert values == ["+1 312 5550199", "a@example.invalid"]


@pytest.mark.parametrize(
    ("field", "xtn", "use"),
    [
        ("f14", "5550199^EMR^PH", None),
        ("f13", "5550199^EMR^PH", None),
        ("f14", "5550199^^PH", "work"),
        ("f13", "5550199^^PH", "home"),
        ("f14", "5550199^WPN^PH", "work"),
        ("f13", "5550199^PRS^CP", "mobile"),
    ],
)
def test_the_field_default_use_applies_only_when_xtn2_is_empty(field, xtn, use, registry):
    contact = _patient(_adt("SC9001", **{field: xtn}), registry)["telecom"][0]
    assert contact.get("use") == use
