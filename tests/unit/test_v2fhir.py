"""v2 to FHIR mapping (hl7bridge.v2fhir): golden Bundles, the mapping spec
and its drift checks, the result key (observation_identifier), value types
(obx_value), the code tables, and Encounter from PV1.

Golden files live in tests/golden/to-fhir. After an intended change, run
with HL7BRIDGE_UPDATE_GOLDEN=1 once and review the diff.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import ConfigError, MappingError
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.v2fhir import bundle_json, message_to_bundle, tables
from hl7bridge.v2fhir import spec as spec_module

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "to-fhir"
UPDATE = os.environ.get("HL7BRIDGE_UPDATE_GOLDEN") == "1"
V2FHIR_SRC = REPO / "src" / "hl7bridge" / "v2fhir"

REFUSED_SAMPLES = {
    "08_hospc_adt_a40_merge.hl7": "MSH-9: message type is not mapped to FHIR by hl7bridge",
    "10_hospc_ack_r01.hl7": "MSH-9: message type is not mapped to FHIR by hl7bridge",
    "11_broken_missing_accession.hl7": (
        "ORC-3: no accession in the field the hospital profile names"
    ),
    "12_broken_bad_timestamp.hl7": "OBR-22: not a valid HL7 timestamp",
}


@pytest.fixture(scope="module")
def registry() -> HospitalRegistry:
    return HospitalRegistry.load_dir(BUNDLED_PROFILES)


def _samples() -> list[Path]:
    return sorted(SAMPLES_DIR.glob("*.hl7"))


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


# -- messages built for one case each ----------------------------------------------

MSH_A = "MSH|^~\\&|RIS|HOSPA|HL7BRIDGE|HUB|20261007120000||{type}|T{n}|T|2.3"
PID_A = "PID|1||SA9001^^^HOSPA^MR||ZZZTEST^ALPHA||19800101|F"
OBR_A = (
    "OBR|1|PL1|ACC9001|71046^XR CHEST^local|R||20261007110000|||||||||||||||20261007120000||CR|F"
)


def oru(*obx: str, obr: str = OBR_A, extra: tuple[str, ...] = ()) -> str:
    segments = [MSH_A.format(type="ORU^R01", n=1), PID_A, "ORC|RE|PL1|", obr, *obx, *extra]
    return "\r".join(segments) + "\r"


def adt(event: str, pv1: str, pid: str = PID_A) -> str:
    msh = f"MSH|^~\\&|ADT|HOSPC|HL7BRIDGE|HUB|20261007120000-0400||ADT^{event}|C{event}|T|2.5"
    pid = pid.replace("HOSPA", "HOSPC")
    return "\r".join([msh, f"EVN|{event}|20261007120000-0400", pid, pv1]) + "\r"


def pv1(**fields: str) -> str:
    values = [""] * 46
    values[0] = "PV1"
    values[1] = "1"
    for key, value in fields.items():
        values[int(key[1:])] = value
    return "|".join(values).rstrip("|")


def resources(text: str, registry: HospitalRegistry) -> list[dict]:
    return [e["resource"] for e in bundle_json(message_to_bundle(text, registry))["entry"]]


def of_type(found: list[dict], kind: str) -> list[dict]:
    return [r for r in found if r["resourceType"] == kind]


EXTRA_FIXTURES = {
    "address_and_email": "\r".join(
        [
            MSH_A.format(type="ADT^A04", n=2),
            "PID|1||SA9002^^^HOSPA^MR||ZZZTEST^BETA^C||19800101|M|||"
            "2 TEST RD^UNIT 3^TESTVILLE^ZZ^00000^ZZ||^NET^Internet^zzz@example.invalid",
            "PV1|1|O",
        ]
    )
    + "\r",
    "order_from_obr2": "\r".join(
        [
            MSH_A.format(type="ORM^O01", n=3),
            PID_A,
            "ORC|NW||||||||",
            "OBR|1|PLX|ACC9002|71046^XR CHEST^local|R||20261007110000",
        ]
    )
    + "\r",
    "every_value_type": oru(
        "OBX|1|NM|N1^Num^99HUB|1|5|kg^kilogram^LOCAL|1-9|H~A|||F|||20261007110500",
        "OBX|2|ST|S1^Str^99HUB||one~two||||||P",
        "OBX|3|DT|D1^Date^99HUB||20261001||||||C",
        "OBX|4|DTM|T1^Time^99HUB||20261001101500||||||F",
        "NTE|1||note one~note line two",
        "NTE|2||note two",
    ),
}


# -- golden Bundles ---------------------------------------------------------------


@pytest.mark.parametrize("path", _samples(), ids=lambda p: p.name)
def test_samples_map_to_their_golden_bundle(path, registry):
    if path.name in REFUSED_SAMPLES:
        with pytest.raises(MappingError) as info:
            message_to_bundle(_text(path), registry)
        assert str(info.value) == REFUSED_SAMPLES[path.name]
        return
    got = json.dumps(bundle_json(message_to_bundle(_text(path), registry)), indent=2) + "\n"
    golden = GOLDEN / (path.stem + ".json")
    if UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_bytes(got.encode("utf-8"))
    assert golden.read_text(encoding="utf-8") == got


def test_no_stale_golden_files():
    expected = {p.stem + ".json" for p in _samples() if p.name not in REFUSED_SAMPLES}
    assert {p.name for p in GOLDEN.glob("*.json")} == expected


def test_same_message_gives_the_same_bundle(registry):
    path = SAMPLES_DIR / "14_hospa_oru_r01_structured.hl7"
    first = bundle_json(message_to_bundle(_text(path), registry))
    assert bundle_json(message_to_bundle(_text(path), registry)) == first


def test_entries_are_conditional_updates_with_resolvable_references(registry):
    mapped = bundle_json(
        message_to_bundle(_text(SAMPLES_DIR / "14_hospa_oru_r01_structured.hl7"), registry)
    )
    assert mapped["type"] == "transaction"
    urls = {e["fullUrl"] for e in mapped["entry"]}
    for entry in mapped["entry"]:
        ident = entry["resource"]["identifier"][0]
        kind = entry["resource"]["resourceType"]
        assert entry["request"] == {
            "method": "PUT",
            "url": f"{kind}?identifier={ident['system']}|{ident['value']}",
        }
        assert entry["fullUrl"].startswith("urn:uuid:")
    report = of_type([e["resource"] for e in mapped["entry"]], "DiagnosticReport")[0]
    assert {r["reference"] for r in report["result"]} <= urls
    assert report["subject"]["reference"] in urls


def test_identifier_values_are_percent_encoded_in_the_request_url(registry):
    text = _text(SAMPLES_DIR / "02_hospa_orm_o01.hl7").replace("SACCA0001", "ACC A?B")
    entries = bundle_json(message_to_bundle(text, registry))["entry"]
    assert entries[1]["request"]["url"].endswith("|ACC%20A%3FB")


# -- refusals: never guess -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not hl7 at all", "MSH: not parseable as HL7 v2"),
        (
            _text(SAMPLES_DIR / "01_hospa_adt_a04.hl7").replace("|HOSPA|", "|NOWHERE|"),
            "MSH-4/MSH-6: match no configured hospital",
        ),
        (
            _text(SAMPLES_DIR / "01_hospa_adt_a04.hl7").replace("ADT^A04", "ADT^A01"),
            "MSH-9: message type is not listed in the hospital profile",
        ),
        (
            _text(SAMPLES_DIR / "01_hospa_adt_a04.hl7").replace("ADT^A04", "ADT^A99"),
            "MSH-9: message type is not mapped to FHIR by hl7bridge",
        ),
        (
            _text(SAMPLES_DIR / "01_hospa_adt_a04.hl7").replace("SA1001^^^HOSPA^MR", ""),
            "PID-3: no patient identifier; a Patient needs one as its key",
        ),
        (
            _text(SAMPLES_DIR / "01_hospa_adt_a04.hl7").replace("|F\r", "|Q\r"),
            "PID-8: code not in table 0001 (administrative sex); refusing to guess",
        ),
        (
            _text(SAMPLES_DIR / "01_hospa_adt_a04.hl7").replace("19800101", "1980"),
            "PID-7: not a valid HL7 date",
        ),
        (
            _text(SAMPLES_DIR / "02_hospa_orm_o01.hl7").replace("ORC|NW|", "ORC|XO|"),
            "ORC-1: code not in table 0119 (order control); refusing to guess",
        ),
        (
            _text(SAMPLES_DIR / "03_hospa_oru_r01_final.hl7").replace("|CR|F", "|CR|Q"),
            "OBR-25: code not in table 0123 (result status); refusing to guess",
        ),
        (
            _text(SAMPLES_DIR / "03_hospa_oru_r01_final.hl7").replace("|CR|F", "|CR|"),
            "OBR-25: no result status; DiagnosticReport.status is required",
        ),
        (
            oru("OBX|1|TX|RPT^Text||x||||||B"),
            "OBX-11: code not in table 0085 (observation result status); refusing to guess",
        ),
        (
            oru("OBX|1|TX|RPT^Text||x||||||"),
            "OBX-11: no result status; Observation.status is required",
        ),
        (oru("OBX|1|TX|^Text||x||||||F"), "OBX-3: no observation code; it is part of the key"),
        (
            oru("OBX|1|NM|N^Num|1|5|||ZZ|||F"),
            "OBX-8: code not in table 0078 (interpretation codes); refusing to guess",
        ),
        (
            oru("OBX|1|SN|N^Num|1|>^100||||||F"),
            "OBX-2: code not in table 0125 (value type); refusing to guess",
        ),
        (oru("OBX|1||N^Num|1|5||||||F"), "OBX-2: OBX-5 has a value but no value type"),
        (
            oru("OBX|1|NM|N^Num|1|>100||||||F"),
            "OBX-5: OBX-2 is NM but the value is not a plain number",
        ),
        (
            oru("OBX|1|NM|N^Num|1|1e3||||||F"),
            "OBX-5: OBX-2 is NM but the value is not a plain number",
        ),
        (
            oru("OBX|1|NM|N^Num|1|1~2||||||F"),
            "OBX-5: repeats, which is mapped only for text types, not NM",
        ),
        (oru("OBX|1|DT|N^Num|1|2026||||||F"), "OBX-5: not a valid HL7 date"),
        (
            oru("OBX|1|CWE|N^Num^bad system!|1|A||||||F"),
            "OBX-3: coding system name is not a plain name",
        ),
        (
            oru("OBX|1|TX|RPT^T||a||||||F", "OBX|1|TX|RPT^T||b||||||F"),
            "OBX-3/OBX-4: two results in one message share a key (code and sub-id)",
        ),
        (
            oru("OBX||TX|RPT^T||a||||||F"),
            "OBX-4: no sub-id and no OBX-1 set id; the result has no key",
        ),
        (
            oru("OBX|1|TX|R/1^T||a||||||F"),
            "OBX-3: holds a character that cannot be part of a result key",
        ),
        (oru("OBX|1|TX|RPT^T|a|b||||||F"), None),  # a plain sub-id is fine
        (
            oru("OBX|1|TX|RPT^T||a||||||F", extra=("OBR|2|PL2|ACC2|1^X^local",)),
            "OBR: an ORU must hold exactly one OBR; more are not mapped",
        ),
        (adt("A01", pv1(p19="V1")), "PV1-2: no patient class; Encounter.class is required"),
        (
            adt("A01", pv1(p2="Z", p19="V1")),
            "PV1-2: code not in table 0004 (patient class); refusing to guess",
        ),
        (adt("A01", pv1(p2="I", p19="V1", p44="2026")), "PV1-44: not a valid HL7 timestamp"),
    ],
)
def test_refusals_name_the_field_and_never_the_value(text, message, registry):
    if message is None:
        message_to_bundle(text, registry)
        return
    with pytest.raises(MappingError) as info:
        message_to_bundle(text, registry)
    assert str(info.value) == message


def test_mapping_errors_cannot_quote_a_value(registry):
    """Every refusal is built from a field position and fixed text; plant a
    canary in each refused field and check it never reaches the message."""
    canary = "CANARYZZ9"
    cases = [
        _text(SAMPLES_DIR / "01_hospa_adt_a04.hl7").replace("|F\r", f"|{canary}\r"),
        _text(SAMPLES_DIR / "02_hospa_orm_o01.hl7").replace("ORC|NW|", f"ORC|{canary}|"),
        oru(f"OBX|1|NM|N^Num|1|{canary}||||||F"),
        oru(f"OBX|1|TX|RPT^Text||x||||||{canary}"),
        oru(f"OBX|1|{canary}|N^Num|1|5||||||F"),
        oru(f"OBX|1|NM|N^Num|1|5|||{canary}|||F"),
        oru(f"OBX|1|TX|RPT^Text|{canary}/1|x||||||F"),
        adt("A01", pv1(p2=canary, p19="V1")),
        adt("A01", pv1(p2="I", p19="V1", p44=canary)),
    ]
    for text in cases:
        with pytest.raises(MappingError) as info:
            message_to_bundle(text, registry)
        assert canary not in str(info.value)
        assert canary not in repr(info.value.args)


def test_mapping_error_takes_no_value_argument():
    import inspect

    params = list(inspect.signature(MappingError.__init__).parameters)
    assert params == ["self", "field", "reason", "segment"]


# -- the result key (T3) -------------------------------------------------------------


def test_key_uses_the_sub_id_then_the_set_id(registry):
    found = resources(
        oru(
            "OBX|1|NM|N1^Num^99HUB|2|5||||||F",
            "OBX|2|TX|RPT^T||line one||||||F",
            "OBX|3|TX|RPT^T||line two||||||F",
        ),
        registry,
    )
    keys = [o["identifier"][0]["value"] for o in of_type(found, "Observation")]
    assert keys == ["ACC9001/N1/2", "ACC9001/RPT/set-2", "ACC9001/RPT/set-3"]
    assert {o["identifier"][0]["system"] for o in of_type(found, "Observation")} == {
        "urn:hl7bridge:hospa:observation"
    }


def test_same_code_with_different_sub_ids_are_two_results(registry):
    found = resources(oru("OBX|1|NM|N1^Num|a|5||||||F", "OBX|2|NM|N1^Num|b|6||||||F"), registry)
    assert [o["identifier"][0]["value"] for o in of_type(found, "Observation")] == [
        "ACC9001/N1/a",
        "ACC9001/N1/b",
    ]


@pytest.mark.parametrize("bad", ["/", "|", ",", "$"])
def test_key_parts_with_token_separators_are_refused(bad, registry):
    escaped = {"|": "\\F\\"}.get(bad, bad)
    with pytest.raises(MappingError, match="OBX-4: holds a character"):
        message_to_bundle(oru(f"OBX|1|TX|RPT^T|a{escaped}b|x||||||F"), registry)


def test_accession_with_a_separator_is_refused_as_a_key(registry):
    text = oru("OBX|1|TX|RPT^T||x||||||F").replace("ACC9001", "ACC/9001")
    with pytest.raises(MappingError, match="accession: holds a character"):
        message_to_bundle(text, registry)


# -- values (T4) ---------------------------------------------------------------------


def test_every_value_type(registry):
    found = of_type(resources(EXTRA_FIXTURES["every_value_type"], registry), "Observation")
    nm, st, dt, dtm = found
    assert nm["valueQuantity"] == {"value": 5, "unit": "kilogram"}  # not UCUM: no code
    assert nm["referenceRange"] == [{"text": "1-9"}]
    assert [i["coding"][0]["code"] for i in nm["interpretation"]] == ["H", "A"]
    assert nm["effectiveDateTime"] == "2026-10-07T18:05:00Z"
    assert dtm["note"] == [{"text": "note one\nnote line two"}, {"text": "note two"}]
    assert st["valueString"] == "one\ntwo"
    assert st["status"] == "preliminary"
    assert dt["valueDateTime"] == "2026-10-01"
    assert dt["status"] == "corrected"
    assert dtm["valueDateTime"] == "2026-10-01T17:15:00Z"
    assert "note" not in nm and "note" not in st and "note" not in dt


def test_ucum_units_get_system_and_code(registry):
    found = of_type(
        resources(oru("OBX|1|NM|N1^Num|1|-0.50|mm^millimeter^UCUM|||||F"), registry),
        "Observation",
    )
    assert found[0]["valueQuantity"] == {
        "value": -0.5,
        "unit": "millimeter",
        "system": "http://unitsofmeasure.org",
        "code": "mm",
    }


def test_numbers_keep_their_precision(registry):
    from hl7bridge.v2fhir.bundle import message_to_bundle as mtb

    mapped = mtb(oru("OBX|1|NM|N1^Num|1|12.50||||||F"), registry)
    obs = next(
        e.resource for e in mapped.bundle.entry if e.resource.get_resource_type() == "Observation"
    )
    assert str(obs.valueQuantity.value) == "12.50"


def test_coded_value_with_alternate_and_original_text(registry):
    found = of_type(
        resources(
            oru("OBX|1|CWE|F1^Find^LN|1|A1^Alpha^SCT^B2^Beta^LOCAL^^^Seen by eye||||||F"),
            registry,
        ),
        "Observation",
    )
    assert found[0]["code"]["coding"][0]["system"] == "http://loinc.org"
    assert found[0]["valueCodeableConcept"] == {
        "coding": [
            {"system": "http://snomed.info/sct", "code": "A1", "display": "Alpha"},
            {"system": "urn:hl7bridge:coding-system:LOCAL", "code": "B2", "display": "Beta"},
        ],
        "text": "Seen by eye",
    }


def test_empty_value_gives_no_value_element(registry):
    found = of_type(resources(oru("OBX|1|NM|N1^Num|1|||||||X"), registry), "Observation")
    assert not any(k.startswith("value") for k in found[0])
    assert found[0]["status"] == "cancelled"


@pytest.mark.parametrize(("code", "status"), sorted(tables.OBSERVATION_STATUS.codes.items()))
def test_observation_status_table(code, status, registry):
    found = of_type(resources(oru(f"OBX|1|TX|RPT^T||x||||||{code}"), registry), "Observation")
    assert found[0]["status"] == status


@pytest.mark.parametrize("code", ["B", "I", "N", "O", "R", "S", "U", "V"])
def test_observation_status_codes_the_ig_leaves_unmapped_are_refused(code, registry):
    with pytest.raises(MappingError, match="OBX-11: code not in table 0085"):
        message_to_bundle(oru(f"OBX|1|TX|RPT^T||x||||||{code}"), registry)


@pytest.mark.parametrize(("code", "status"), sorted(tables.REPORT_STATUS.codes.items()))
def test_report_status_table(code, status, registry):
    text = _text(SAMPLES_DIR / "03_hospa_oru_r01_final.hl7").replace("|CR|F", f"|CR|{code}")
    assert of_type(resources(text, registry), "DiagnosticReport")[0]["status"] == status


# -- DiagnosticReport ---------------------------------------------------------------


def test_report_text_is_conclusion_and_presented_form(registry):
    import base64

    found = resources(_text(SAMPLES_DIR / "14_hospa_oru_r01_structured.hl7"), registry)
    report = of_type(found, "DiagnosticReport")[0]
    text = "Synthetic structured report. Measurement within range."
    assert report["conclusion"] == text
    assert report["presentedForm"][0]["contentType"] == "text/plain; charset=utf-8"
    assert base64.b64decode(report["presentedForm"][0]["data"]).decode() == text
    assert report["effectiveDateTime"] == "2026-10-07T18:00:00Z"  # OBR-7
    assert report["issued"] == "2026-10-07T19:00:00Z"  # OBR-22
    assert len(report["result"]) == 3
    assert report["basedOn"][0]["identifier"]["value"] == "SACCA0002"
    assert report["basedOn"][0]["type"] == "ServiceRequest"


# -- Encounter from PV1 --------------------------------------------------------------


def test_no_visit_number_no_encounter(registry):
    found = resources(_text(SAMPLES_DIR / "07_hospc_adt_a04_offsets.hl7"), registry)
    assert [r["resourceType"] for r in found] == ["Patient"]


@pytest.mark.parametrize(("code", "value"), sorted(tables.PATIENT_CLASS.codes.items()))
def test_patient_class_table(code, value, registry):
    enc = of_type(resources(adt("A01", pv1(p2=code, p19="V1")), registry), "Encounter")[0]
    system, class_code = value.split("|")
    assert enc["class"] == {"system": system, "code": class_code}
    assert enc["status"] == tables.ENCOUNTER_STATUS.codes[code]


def test_a08_updates_the_same_encounter_and_discharge_finishes_it(registry):
    a01 = bundle_json(
        message_to_bundle(_text(SAMPLES_DIR / "13_hospc_adt_a01_inpatient.hl7"), registry)
    )
    a08 = bundle_json(
        message_to_bundle(_text(SAMPLES_DIR / "15_hospc_adt_a08_discharge.hl7"), registry)
    )
    enc1, enc8 = a01["entry"][1], a08["entry"][1]
    assert enc1["fullUrl"] == enc8["fullUrl"]
    assert enc1["request"] == enc8["request"]
    assert enc1["resource"]["status"] == "in-progress"
    assert enc8["resource"]["status"] == "finished"
    assert enc8["resource"]["period"] == {
        "start": "2026-10-07T15:55:00Z",
        "end": "2026-10-09T13:30:00Z",
    }


def test_profile_identifier_systems_reach_every_resource(tmp_path):
    for source in BUNDLED_PROFILES.glob("*.yaml"):
        text = source.read_text(encoding="utf-8")
        if source.name == "hospa.yaml":
            text += (
                "fhir:\n  identifier_systems:\n"
                "    mrn: http://example.test/mrn\n"
                "    accession: http://example.test/acc\n"
                "    observation: http://example.test/obs\n"
            )
        (tmp_path / source.name).write_text(text, encoding="utf-8")
    custom = HospitalRegistry.load_dir(tmp_path)
    found = resources(_text(SAMPLES_DIR / "14_hospa_oru_r01_structured.hl7"), custom)
    systems = {r["resourceType"]: r["identifier"][0]["system"] for r in found}
    assert systems == {
        "Patient": "http://example.test/mrn",
        "Observation": "http://example.test/obs",
        "DiagnosticReport": "http://example.test/acc",
    }


@pytest.mark.parametrize(
    "value", ["urn:a|b", "urn:a,b", "urn:a$b", "urn:a\\\\b", "urn:a b", "ftp://x", "plain", "7"]
)
def test_bad_identifier_system_is_refused(value, tmp_path):
    source = (BUNDLED_PROFILES / "hospa.yaml").read_text(encoding="utf-8")
    path = tmp_path / "hospa.yaml"
    path.write_text(
        source + f'fhir:\n  identifier_systems:\n    mrn: "{value}"\n', encoding="utf-8"
    )
    from hl7bridge.config import load_hospital_config

    with pytest.raises(ConfigError, match=r"fhir.identifier_systems.mrn must be"):
        load_hospital_config(path)


def test_unknown_fhir_profile_keys_are_refused(tmp_path):
    from hl7bridge.config import load_hospital_config

    source = (BUNDLED_PROFILES / "hospa.yaml").read_text(encoding="utf-8")
    path = tmp_path / "hospa.yaml"
    path.write_text(source + "fhir:\n  identifier_system: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Did you mean 'identifier_systems'"):
        load_hospital_config(path)
    path.write_text(source + "fhir:\n  identifier_systems:\n    mnr: urn:x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Did you mean 'mrn'"):
        load_hospital_config(path)


# -- the spec and its drift checks ---------------------------------------------------


def _all_fixture_texts() -> list[str]:
    texts = [_text(p) for p in _samples() if p.name not in REFUSED_SAMPLES]
    return texts + list(EXTRA_FIXTURES.values())


def test_mapping_doc_matches_the_spec():
    doc = (REPO / "docs" / "mapping.md").read_text(encoding="utf-8")
    assert doc == spec_module.render_markdown(), "run: python tools/gen_mapping_doc.py"


def test_no_mapper_reads_a_field_the_spec_does_not_list(registry):
    allowed = spec_module.fields_by_mapper()
    for text in _all_fixture_texts():
        for mapper, refs in message_to_bundle(text, registry).touched.items():
            assert mapper in allowed, mapper
            extra = refs - allowed[mapper]
            assert not extra, f"{mapper} reads {sorted(extra)} with no row in spec.py"


def test_every_spec_row_is_read_by_some_fixture(registry):
    touched: dict[str, set[str]] = {}
    for text in _all_fixture_texts():
        for mapper, refs in message_to_bundle(text, registry).touched.items():
            touched.setdefault(mapper, set()).update(refs)
    for mapper, refs in spec_module.fields_by_mapper().items():
        unread = refs - touched.get(mapper, set())
        # The accession rows cover every allowed field; the bundled
        # profiles use two of them.
        unread -= {"OBR-2", "ORC-2"} & unread if mapper == "obr_to_diagnostic_report" else set()
        unread -= {"OBR-2"} & unread if mapper == "orc_obr_to_service_request" else set()
        assert not unread, f"spec rows for {mapper} that no fixture reads: {sorted(unread)}"


def test_spec_rows_are_unique_and_mappers_known():
    seen = set()
    for row in spec_module.ROWS:
        assert row.mapper in spec_module.MAPPERS
        assert (row.mapper, row.v2, row.fhir) not in seen, row
        seen.add((row.mapper, row.v2, row.fhir))


def test_mappers_read_fields_only_through_the_reader():
    """A mapper that called get_field or the parsed message directly would
    escape the spec check, so no module but fields.py may."""
    banned = {"get_field", "extract_field", "segments", "get_segment_count"}
    for path in V2FHIR_SRC.glob("*.py"):
        if path.name == "fields.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert not (names & banned), f"{path.name} uses {sorted(names & banned)}"


def test_every_table_value_is_written_as_its_ig_map_says():
    """Spot checks against the IG vocabulary maps, so a slip in a table edit
    shows up here and not only in a diff."""
    assert tables.SEX.codes["A"] == "other" and tables.SEX.codes["N"] == "other"
    assert tables.PATIENT_CLASS.codes["O"].endswith("|AMB")
    assert tables.ENCOUNTER_STATUS.codes["P"] == "planned"
    assert tables.OBSERVATION_STATUS.codes["W"] == "entered-in-error"
    assert "N" not in tables.OBSERVATION_STATUS.codes
    assert tables.REPORT_STATUS.codes["R"] == "partial"
    assert "TOX" not in tables.INTERPRETATION.codes
