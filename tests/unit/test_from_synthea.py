"""from-synthea: radiology HL7 v2 from Synthea-style FHIR Bundles.

The fixtures in tests/fixtures/synthea are hand-written in Synthea's shape
(identifier systems, urn:uuid references, ImagingStudy series) with
obviously fake people. Every message the generator writes must parse, get
AA from the store listener and from the direct-to-FHIR listener, map to
FHIR and lint clean."""

from __future__ import annotations

import base64
import copy
import json
import os
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hl7bridge import cli, demofiles
from hl7bridge.config import ALLOWED_ACCESSION_FIELDS, HospitalRegistry
from hl7bridge.demofiles import OwnedFiles
from hl7bridge.errors import DemoDirError, UsageError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.runscope import RunScope
from hl7bridge.hl7time import parse_hl7_ts
from hl7bridge.ingest import IngestPipeline
from hl7bridge.ingest.direct import DirectHandler, DirectLog
from hl7bridge.ingest.parser import get_field, get_message_type, parse_hl7
from hl7bridge.lint.runner import lint_text
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.store import connect
from hl7bridge.synth import synthea
from hl7bridge.synth.synthea import (
    INDEX_NAME,
    SKIP_BIRTH_DATE,
    SKIP_CLASS,
    SKIP_CODE,
    SKIP_DUPLICATE,
    SKIP_INPATIENT,
    SKIP_MODALITY,
    SKIP_NO_ID,
    SKIP_NO_PATIENT,
    SKIP_ORDER,
    SKIP_PATIENT_SHAPE,
    SKIP_REPEATED_HOUR,
    SKIP_REPORT_COUNT,
    SKIP_REPORT_STATUS,
    SKIP_REPORT_TEXT,
    SKIP_STARTED,
    SKIP_TIME,
    SKIP_TOO_LARGE,
    SKIP_UNSAFE_MRN,
    SYNTHETIC_LINE,
    generate_from_synthea,
    write_run,
)
from hl7bridge.v2fhir import bundle_json, message_to_bundle

from ..fakefhir import FakeFhir

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "synthea"
MRN_SYSTEM = "http://hospital.smarthealthit.org"
TAG = ("urn:test:tag", "synthea-lab")
BASE = "http://fhir.test/fhir"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
ALPHA = "00000000-5e1d-4a00-8000-000000000001"
CT_STUDY = "ImagingStudy/00000000-5e1d-4a00-8000-000000000022"
CR_STUDY = "ImagingStudy/00000000-5e1d-4a00-8000-000000000021"
MR_STUDY = "ImagingStudy/00000000-5e1d-4a00-8000-000000000023"
US_STUDY = "ImagingStudy/00000000-5e1d-4a00-8000-000000000024"


def _profile_text(
    *,
    accession_field: str = "OBR-3",
    version: str = "2.5.1",
    offset: bool = True,
    types: str = "[ADT^A01, ADT^A04, ADT^A08, ORM^O01, ORU^R01]",
    timezone: str = "America/New_York",
    facility: str = "SYNLAB",
    by_authority: str = "",
) -> str:
    authorities = f"\n    mrn_by_authority: {by_authority}" if by_authority else ""
    return f"""hospital_id: SYNLAB
display_name: Synthea lab
facility_codes: [{facility}]
accession_field: {accession_field}
timezone: {timezone}
hl7_version: "{version}"
timestamps_include_offset: {"true" if offset else "false"}
expected_message_types: {types}
fhir:
  patient_policy: match_only
  identifier_systems:
    mrn: {MRN_SYSTEM}
    accession: urn:test:acc
    placer: urn:test:placer
    visit: urn:test:visit
    observation: urn:test:obs{authorities}
  tag: {{system: {TAG[0]}, code: {TAG[1]}}}
"""


def _registry(tmp_path: Path, **kwargs) -> HospitalRegistry:
    folder = tmp_path / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "lab.yaml").write_text(_profile_text(**kwargs), encoding="utf-8")
    return HospitalRegistry.load_dir(folder)


@pytest.fixture
def registry(tmp_path) -> HospitalRegistry:
    return _registry(tmp_path)


@pytest.fixture
def hospital(registry):
    return registry.by_id["SYNLAB"]


def _messages(run) -> list[tuple[str, str]]:
    """(file name, text) for every .hl7 file, in feed order."""
    return [(n, d.decode("utf-8")) for n, d in run.files.items() if n.endswith(".hl7")]


def _bundles(tmp_path: Path, *names: str) -> Path:
    """A folder holding copies of the named fixtures."""
    folder = tmp_path / "in"
    folder.mkdir(exist_ok=True)
    for name in names:
        shutil.copy(FIXTURES / name, folder / name)
    return folder


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _write(folder: Path, name: str, data: dict) -> Path:
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(json.dumps(data).encode("utf-8"))
    return path


def _resource(bundle: dict, kind: str, n: int = 0) -> dict:
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == kind][n]


# -- the fixtures as a whole ---------------------------------------------------------------


def test_the_fixtures_give_the_expected_feed(hospital):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    assert (run.bundles, run.studies, run.skipped) == (4, 4, [])
    assert dict(run.counts) == {"ADT^A01": 1, "ADT^A04": 3, "ORM^O01": 4, "ORU^R01": 4}
    assert [s["imaging_study"] for s in run.index["studies"]] == [
        CR_STUDY,
        MR_STUDY,
        CT_STUDY,
        US_STUDY,
    ]
    names = [n for n, _ in _messages(run)]
    assert names == sorted(names) and names[0] == "00001_adt_a04.hl7"
    for study in run.index["studies"]:
        assert study["adt_file"] < study["order_file"] < study["report_file"]
        assert len(study["accession"]) == 16 and study["accession"].startswith("A")
    assert json.loads(run.files[INDEX_NAME]) == run.index


def test_every_message_gets_aa_from_the_store_listener(tmp_path, registry):
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    conn = connect(tmp_path / "store.db")
    pipeline = IngestPipeline(registry, conn)
    for name, text in _messages(run):
        parse_hl7(text)
        ack = pipeline.handle_bytes(text.encode("utf-8"))
        assert "\rMSA|AA|" in ack, name
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("patients", "orders", "reports", "encounters")
    }
    assert counts == {"patients": 3, "orders": 4, "reports": 4, "encounters": 4}
    accessions = {r[0] for r in conn.execute("SELECT accession FROM orders")}
    assert accessions == {s["accession"] for s in run.index["studies"]}
    conn.close()


def test_every_message_maps_to_fhir_and_lints_clean(registry):
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    for name, text in _messages(run):
        assert lint_text(text, registry) == [], name
        mapped = message_to_bundle(text, registry)
        kinds = mapped.entry_types()
        msg_type = get_message_type(parse_hl7(text))
        if msg_type == "ORU^R01":
            report = bundle_json(mapped)["entry"][-1]["resource"]
            assert report["resourceType"] == "DiagnosticReport"
            assert report["conclusion"].endswith(SYNTHETIC_LINE)
        elif msg_type == "ORM^O01":
            assert "ServiceRequest" in kinds
        else:
            assert kinds[0] == "Patient"


def test_a_linked_report_keeps_its_text_and_status(registry):
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    study = next(s for s in run.index["studies"] if s["imaging_study"] == MR_STUDY)
    assert (study["report_status"], study["report_text"]) == ("P", "source")
    text = run.files[study["report_file"]].decode()
    report = bundle_json(message_to_bundle(text, registry))["entry"][-1]["resource"]
    assert report["status"] == "preliminary"
    assert report["conclusion"].split("\n") == [
        "MRI BRAIN WITHOUT CONTRAST",
        "FINDINGS: Synthetic preliminary read, no acute finding.",
        "IMPRESSION: Synthetic | text with a pipe.",
        SYNTHETIC_LINE,
    ]
    assert report["issued"].startswith("2026-05-11T16:02:00")


def test_a_clinical_note_on_the_encounter_is_never_the_report(hospital):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    study = next(s for s in run.index["studies"] if s["imaging_study"] == CR_STUDY)
    assert study["report_text"] == "generated"
    assert "history and physical" not in run.files[study["report_file"]].decode().lower()


def test_the_service_request_gives_code_priority_and_order_time(hospital):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    study = next(s for s in run.index["studies"] if s["imaging_study"] == CT_STUDY)
    orm = parse_hl7(run.files[study["order_file"]].decode())
    assert get_field(orm, "OBR-4", component=1) == "303653007"
    assert get_field(orm, "OBR-4", component=3) == "SCT"
    assert get_field(orm, "OBR-5") == "S"
    assert get_field(orm, "ORC-9") == "20260719222000-0400"
    assert get_field(orm, "OBR-24") == "CT"
    adt = parse_hl7(run.files[study["adt_file"]].decode())
    assert get_field(adt, "PV1-2") == "E" and get_field(adt, "PV1-19") == study["visit"]
    assert get_field(adt, "PV1-44") == "20260719220500-0400"


def test_an_inpatient_encounter_gives_an_a01(hospital):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    study = next(s for s in run.index["studies"] if s["imaging_study"] == MR_STUDY)
    assert study["adt_file"].endswith("_adt_a01.hl7")
    adt = parse_hl7(run.files[study["adt_file"]].decode())
    assert get_field(adt, "PV1-2") == "I"


def test_a_study_with_no_encounter_is_its_own_outpatient_visit(hospital):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    study = next(s for s in run.index["studies"] if s["imaging_study"] == US_STUDY)
    assert study["visit"].startswith("V") and len(study["visit"]) == 16
    adt = parse_hl7(run.files[study["adt_file"]].decode())
    assert get_field(adt, "PV1-2") == "O" and get_field(adt, "PV1-19") == study["visit"]
    assert get_field(adt, "PV1-44") == ""


def test_two_studies_in_one_visit_share_one_adt(tmp_path, hospital):
    bundle = _load("zzztest_alpha.json")
    second = copy.deepcopy(_resource(bundle, "ImagingStudy"))
    second["id"] = "00000000-5e1d-4a00-8000-000000000099"
    second["started"] = "2026-03-02T09:55:00-05:00"
    second["encounter"] = {"reference": "Encounter/00000000-5e1d-4a00-8000-000000000011"}
    bundle["entry"].append({"fullUrl": f"urn:uuid:{second['id']}", "resource": second})
    run = generate_from_synthea(_write(tmp_path / "in", "a.json", bundle), hospital, seed=42)
    first, other = run.index["studies"][:2]
    assert first["adt_file"] == other["adt_file"] and first["visit"] == other["visit"]
    assert run.counts["ADT^A04"] == 2  # this visit and the emergency one


@pytest.mark.parametrize("field", ALLOWED_ACCESSION_FIELDS)
def test_the_accession_goes_where_the_profile_reads_it(tmp_path, field):
    registry = _registry(tmp_path, accession_field=field)
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    conn = connect(tmp_path / "store.db")
    pipeline = IngestPipeline(registry, conn)
    for name, text in _messages(run):
        assert "\rMSA|AA|" in pipeline.handle_bytes(text.encode()), name
    assert {r[0] for r in conn.execute("SELECT accession FROM reports")} == {
        s["accession"] for s in run.index["studies"]
    }
    conn.close()


def test_an_old_version_without_offsets_still_gets_aa(tmp_path):
    registry = _registry(tmp_path, version="2.3", offset=False)
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    conn = connect(tmp_path / "store.db")
    pipeline = IngestPipeline(registry, conn)
    for _, text in _messages(run):
        msh = text.split("\r")[0].split("|")
        assert msh[11] == "2.3" and "-" not in msh[6] and "+" not in msh[6]
        assert "\rMSA|AA|" in pipeline.handle_bytes(text.encode())
    conn.close()


# -- the WintEHR lab path: the direct-to-FHIR listener ------------------------------------


def _fhir_patient(bundle_name: str) -> dict:
    patient = _resource(_load(bundle_name), "Patient")
    return {k: v for k, v in patient.items() if k != "id"}


def test_every_message_gets_aa_from_the_direct_listener(tmp_path, registry):
    server = FakeFhir()
    names = ("zzztest_alpha.json", "zzzsample_bravo.json", "qqfake_charlie.json")
    for n, name in enumerate(names, start=1):
        server._write("Patient", f"p{n}", _fhir_patient(name))
    before = copy.deepcopy(server.data["Patient"])
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    with HapiClient(BASE, transport=server.transport()) as client:
        handler = DirectHandler(
            registry,
            client,
            RunScope("lab-a", TAG),
            DirectLog(tmp_path / "d.db"),
            clock=lambda: NOW,
        )
        for name, text in _messages(run):
            ack = handler.handle_bytes(text.encode())
            assert "\rMSA|AA|" in ack, name
    assert server.data["Patient"] == before
    assert {k: len(server.all(k)) for k in ("ServiceRequest", "DiagnosticReport", "Encounter")} == {
        "ServiceRequest": 4,
        "DiagnosticReport": 4,
        "Encounter": 4,
    }


# -- determinism ---------------------------------------------------------------------------


def test_same_input_and_seed_give_the_same_bytes(hospital):
    a = generate_from_synthea(FIXTURES, hospital, seed=42)
    b = generate_from_synthea(FIXTURES, hospital, seed=42)
    assert a.files == b.files


def test_the_seed_moves_times_and_text_but_never_keys(hospital):
    a = generate_from_synthea(FIXTURES, hospital, seed=42)
    b = generate_from_synthea(FIXTURES, hospital, seed=7)
    keys = ("imaging_study", "accession", "visit", "mrn")
    assert [[s[k] for k in keys] for s in a.index["studies"]] == [
        [s[k] for k in keys] for s in b.index["studies"]
    ]
    assert a.files != b.files
    a_ids = {get_field(parse_hl7(t), "MSH-10") for _, t in _messages(a)}
    b_ids = {get_field(parse_hl7(t), "MSH-10") for _, t in _messages(b)}
    assert len(a_ids) == len(_messages(a)) and not a_ids & b_ids

    def seeded(run):
        """Per study: (exam time, order time, report time, report text)."""
        values = {}
        for s in run.index["studies"]:
            orm = parse_hl7(run.files[s["order_file"]].decode())
            oru_text = run.files[s["report_file"]].decode()
            oru = parse_hl7(oru_text)
            obx = [seg for seg in oru_text.split("\r") if seg.startswith("OBX|")]
            values[s["accession"]] = (
                get_field(oru, "OBR-7"),
                get_field(orm, "ORC-9"),
                get_field(oru, "OBR-22"),
                obx,
            )
        return values

    sa, sb = seeded(a), seeded(b)
    assert [v[0] for v in sa.values()] == [v[0] for v in sb.values()]  # exams never move
    for n in (1, 2, 3):  # order time, report time and generated text each move somewhere
        assert any(sa[k][n] != sb[k][n] for k in sa), n


def test_adding_a_patient_changes_no_other_patients_messages(tmp_path, hospital):
    small = generate_from_synthea(
        _bundles(tmp_path, "zzztest_alpha.json", "qqfake_charlie.json"), hospital, seed=42
    )
    full = generate_from_synthea(FIXTURES, hospital, seed=42)

    def by_file(run):
        return {
            (s["accession"], kind): run.files[s[f"{kind}_file"]]
            for s in run.index["studies"]
            for kind in ("adt", "order", "report")
        }

    small_texts = by_file(small)
    full_texts = by_file(full)
    assert all(full_texts[k] == v for k, v in small_texts.items())


def test_the_cli_writes_the_same_bytes_twice(tmp_path, registry, capsys):
    config = str(tmp_path / "profiles")
    for out in ("a", "b"):
        args = ["from-synthea", str(FIXTURES), "--hospital", "SYNLAB", "--out"]
        assert cli.main([*args, str(tmp_path / out), "--config", config]) == 0
    a = {p.name: p.read_bytes() for p in (tmp_path / "a").iterdir()}
    b = {p.name: p.read_bytes() for p in (tmp_path / "b").iterdir()}
    assert a == b and len(a) == 13
    assert all(b"\n" not in data for name, data in a.items() if name.endswith(".hl7"))
    out = capsys.readouterr().out
    assert "read 4 Bundle file(s), 4 imaging study(ies), seed 42" in out
    assert "(ADT^A01 1, ADT^A04 3, ORM^O01 4, ORU^R01 4) and index.json" in out


def test_no_network_is_used(monkeypatch, tmp_path, hospital):
    def refuse(*args, **kwargs):
        raise AssertionError("from-synthea opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    write_run(generate_from_synthea(FIXTURES, hospital, seed=42), tmp_path / "out")
    assert (tmp_path / "out" / INDEX_NAME).is_file()


# -- usage errors: nothing is written ------------------------------------------------------


def _usage(path: Path, hospital, match: str) -> None:
    with pytest.raises(UsageError, match=match):
        generate_from_synthea(path, hospital, seed=42)


def test_a_missing_path_or_an_empty_folder_is_refused(tmp_path, hospital):
    _usage(tmp_path / "nope", hospital, "does not exist")
    (tmp_path / "empty").mkdir()
    _usage(tmp_path / "empty", hospital, r"no \*\.json files under")


def test_a_file_that_is_not_a_bundle_is_refused(tmp_path, hospital):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.json").write_bytes(b"{not json")
    _usage(folder, hospital, "^file 1 is not UTF-8 JSON.$")
    (folder / "a.json").write_bytes(b"\xff\xfe")
    _usage(folder, hospital, "^file 1 is not UTF-8 JSON.$")
    (folder / "a.json").write_bytes(b"[" * 100_000 + b"]" * 100_000)
    _usage(folder, hospital, "^file 1 is not UTF-8 JSON.$")
    _write(folder, "a.json", {"resourceType": "Patient"})
    _usage(folder, hospital, "^file 1 is not a FHIR Bundle.$")
    _write(folder, "a.json", {"resourceType": "Bundle", "entry": [{"fullUrl": "x"}]})
    _usage(folder, hospital, "Bundle.entry is not a list of entries with a resource")


def test_the_size_limits_are_refused(tmp_path, hospital, monkeypatch):
    monkeypatch.setattr(synthea, "MAX_BUNDLE_BYTES", 1024)
    _usage(FIXTURES, hospital, "^file 1 is larger than 1024 bytes.$")
    monkeypatch.setattr(synthea, "MAX_BUNDLE_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(synthea, "MAX_FILES", 3)
    _usage(FIXTURES, hospital, "4 files under .*; the limit is 3.")
    monkeypatch.setattr(synthea, "MAX_FILES", 20_000)
    monkeypatch.setattr(synthea, "MAX_STUDIES", 3)
    _usage(FIXTURES, hospital, "more than 3 imaging studies; the limit is 3.")


def test_bundles_with_no_imaging_study_are_refused(tmp_path, hospital):
    folder = _bundles(tmp_path, "hospitalInformation1700000000000.json")
    _usage(folder, hospital, "no ImagingStudy in the Bundles under")


def test_a_profile_missing_a_needed_type_is_refused(tmp_path):
    registry = _registry(tmp_path, types="[ADT^A04, ORM^O01]")
    _usage(FIXTURES, registry.by_id["SYNLAB"], "profile SYNLAB does not expect ORU\\^R01;")


def test_a_patient_outside_the_profiles_mrn_systems_is_refused_by_file(tmp_path):
    hospa = HospitalRegistry.load_dir(BUNDLED_PROFILES).by_id["HOSPA"]
    with pytest.raises(UsageError, match=r"^file 2: the Patient has no identifier"):
        generate_from_synthea(FIXTURES, hospa, seed=42)


@pytest.mark.parametrize(
    "born",
    ["05/01/1990", "1990", "1990-05", "2023-02-31", "0999-01-01", 19900105, None, 0, False, ""],
)
def test_a_birth_date_pid7_cannot_hold_skips_the_patient(tmp_path, hospital, born):
    bundle = _load("qqfake_charlie.json")
    _resource(bundle, "Patient")["birthDate"] = born
    assert _skips(tmp_path, hospital, bundle) == [SKIP_BIRTH_DATE]


def test_out_must_be_new_and_files_are_never_replaced(tmp_path, hospital):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(UsageError, match="exists and is not a folder"):
        write_run(run, tmp_path / "file")
    write_run(run, tmp_path / "out")
    with pytest.raises(UsageError, match="is not empty; from-synthea never mixes two runs"):
        write_run(run, tmp_path / "out")
    (tmp_path / "empty").mkdir()
    with pytest.raises(UsageError, match="already exists; from-synthea writes a new folder"):
        write_run(run, tmp_path / "empty")


def test_a_usage_error_in_the_last_file_writes_nothing(tmp_path, registry):
    folder = _bundles(tmp_path, "qqfake_charlie.json")
    (folder / "zz_broken.json").write_bytes(b"{")
    args = ["from-synthea", str(folder), "--hospital", "SYNLAB", "--out", str(tmp_path / "out")]
    assert cli.main([*args, "--config", str(tmp_path / "profiles")]) == 2
    assert not (tmp_path / "out").exists()


def test_the_cli_refuses_an_unknown_hospital(tmp_path, registry, capsys):
    args = ["from-synthea", str(FIXTURES), "--hospital", "NOPE", "--out", str(tmp_path / "o")]
    assert cli.main([*args, "--config", str(tmp_path / "profiles")]) == 2
    assert "--hospital names no loaded profile." in capsys.readouterr().err


# -- skipped studies: the rest is written, the run exits 1 --------------------------------


def _skips(tmp_path: Path, hospital, bundle: dict) -> list[str]:
    run = generate_from_synthea(_write(tmp_path / "in", "b.json", bundle), hospital, seed=42)
    return [k.reason for k in run.skipped]


def _alpha_with(change) -> dict:
    bundle = _load("zzztest_alpha.json")
    change(bundle)
    return bundle


def _cr(bundle: dict) -> dict:
    return _resource(bundle, "ImagingStudy", 0)


def _ct(bundle: dict) -> dict:
    return _resource(bundle, "ImagingStudy", 1)


SKIP_CASES = {
    "no patient": (lambda b: _cr(b)["subject"].update(reference="urn:uuid:other"), SKIP_NO_PATIENT),
    "date only": (lambda b: _cr(b).update(started="2026-03-02"), SKIP_STARTED),
    "no offset": (lambda b: _cr(b).update(started="2026-03-02T09:40:00"), SKIP_STARTED),
    "no modality": (
        lambda b: _cr(b)["series"][0]["modality"].update(code="x-ray"),
        SKIP_MODALITY,
    ),
    "encounter class": (
        lambda b: _resource(b, "Encounter")["class"].update(code="WHO"),
        SKIP_CLASS,
    ),
    "encounter time": (
        lambda b: _resource(b, "Encounter")["period"].update(start="2026-03-02"),
        SKIP_TIME,
    ),
    "order after exam": (
        lambda b: _resource(b, "ServiceRequest").update(authoredOn="2026-07-19T23:00:00-04:00"),
        SKIP_ORDER,
    ),
    "year 1": (lambda b: _cr(b).update(started="0001-01-01T00:05:00+00:00"), SKIP_STARTED),
    "year 9999": (lambda b: _cr(b).update(started="9999-12-31T23:59:00+00:00"), SKIP_STARTED),
    "code with control characters": (
        lambda b: _cr(b)["procedureCode"][0]["coding"][0].update(code="A\x00\x00B"),
        SKIP_CODE,
    ),
    "code with two spaces": (
        lambda b: _cr(b)["procedureCode"][0]["coding"][0].update(code="39  9208008"),
        SKIP_CODE,
    ),
    "encounter after exam": (
        lambda b: _resource(b, "Encounter")["period"].update(start="2026-03-02T10:00:00-05:00"),
        SKIP_ORDER,
    ),
}


@pytest.mark.parametrize("case", SKIP_CASES, ids=str)
def test_each_skip_reason_skips_only_that_study(tmp_path, hospital, case):
    change, reason = SKIP_CASES[case]
    assert _skips(tmp_path, hospital, _alpha_with(change)) == [reason]


def _linked_report(**fields) -> dict:
    return {
        "resourceType": "DiagnosticReport",
        "id": "r1",
        "status": "final",
        "imagingStudy": [{"reference": CT_STUDY}],
        **fields,
    }


@pytest.mark.parametrize(
    ("reports", "reason"),
    [
        ([_linked_report(status="registered")], SKIP_REPORT_STATUS),
        ([_linked_report(), _linked_report(id="r2")], SKIP_REPORT_COUNT),
        ([_linked_report(conclusion="x" * (32 * 1024 + 1))], SKIP_REPORT_TEXT),
        ([_linked_report(conclusion="x\n" * 501)], SKIP_REPORT_TEXT),
        (
            [_linked_report(presentedForm=[{"contentType": "text/plain", "data": "@@@"}])],
            SKIP_REPORT_TEXT,
        ),
        (
            [_linked_report(presentedForm=[{"contentType": "text/plain", "data": 12345}])],
            SKIP_REPORT_TEXT,
        ),
        (
            [_linked_report(presentedForm=[{"contentType": "text/plain", "data": ["eA=="]}])],
            SKIP_REPORT_TEXT,
        ),
        (
            [_linked_report(presentedForm=[{"contentType": "text/plain", "data": {"a": 1}}])],
            SKIP_REPORT_TEXT,
        ),
        ([_linked_report(conclusion=12345)], SKIP_REPORT_TEXT),
        ([_linked_report(presentedForm=[{"contentType": 5, "data": "eA=="}])], SKIP_REPORT_TEXT),
        ([_linked_report(issued="2026-07-19T22:00:00-04:00")], SKIP_ORDER),
        ([_linked_report(issued="2026-07-19")], SKIP_TIME),
    ],
)
def test_a_linked_report_that_cannot_be_written_skips_its_study(
    tmp_path, hospital, reports, reason
):
    bundle = _load("zzztest_alpha.json")
    bundle["entry"] += [{"resource": r} for r in reports]
    assert _skips(tmp_path, hospital, bundle) == [reason]


def test_a_report_linked_through_the_service_request_is_used(tmp_path, hospital):
    bundle = _load("zzztest_alpha.json")
    text = base64.b64encode(b"FINDINGS: Linked by order.").decode()
    report = _linked_report(
        imagingStudy=[],
        basedOn=[{"reference": "ServiceRequest/00000000-5e1d-4a00-8000-000000000041"}],
        presentedForm=[{"contentType": "text/plain", "data": text}],
    )
    bundle["entry"].append({"resource": report})
    run = generate_from_synthea(_write(tmp_path / "in", "b.json", bundle), hospital, seed=42)
    study = next(s for s in run.index["studies"] if s["imaging_study"] == CT_STUDY)
    assert study["report_text"] == "source"
    assert (
        "OBX|1|TX|RPT^Report Text||FINDINGS: Linked by order."
        in run.files[study["report_file"]].decode()
    )


def test_an_empty_text_form_is_passed_over_for_the_next(tmp_path, hospital):
    bundle = _load("zzztest_alpha.json")
    text = base64.b64encode(b"FINDINGS: The second form.").decode()
    forms = [{"contentType": "text/plain", "data": ""}, {"contentType": "text/plain", "data": text}]
    bundle["entry"].append({"resource": _linked_report(presentedForm=forms)})
    run = generate_from_synthea(_write(tmp_path / "in", "b.json", bundle), hospital, seed=42)
    study = next(s for s in run.index["studies"] if s["imaging_study"] == CT_STUDY)
    assert study["report_text"] == "source"
    assert "FINDINGS: The second form." in run.files[study["report_file"]].decode()


def test_an_inpatient_study_is_skipped_when_the_profile_has_no_a01(tmp_path):
    registry = _registry(tmp_path, types="[ADT^A04, ORM^O01, ORU^R01]")
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    assert [(k.imaging_study, k.reason) for k in run.skipped] == [(MR_STUDY, SKIP_INPATIENT)]
    assert "ADT^A01" not in run.counts


def test_an_mrn_a_search_key_cannot_hold_skips_the_patient(tmp_path, hospital):
    bundle = _load("qqfake_charlie.json")
    _resource(bundle, "Patient")["identifier"][1]["value"] = "MRN,1"
    assert _skips(tmp_path, hospital, bundle) == [SKIP_UNSAFE_MRN]


def test_the_same_study_twice_is_read_once(tmp_path, hospital):
    folder = _bundles(tmp_path, "qqfake_charlie.json")
    (folder / "sub").mkdir()
    shutil.copy(FIXTURES / "qqfake_charlie.json", folder / "sub" / "again.json")
    run = generate_from_synthea(folder, hospital, seed=42)
    assert [(k.source, k.reason) for k in run.skipped] == [("file 2", SKIP_DUPLICATE)]
    assert run.counts["ORM^O01"] == 1


def test_the_cli_exits_1_and_lists_skips(tmp_path, registry, capsys):
    bundle = _alpha_with(lambda b: _cr(b).update(started="2026-03-02"))
    path = _write(tmp_path / "in", "b.json", bundle)
    args = ["from-synthea", str(path), "--hospital", "SYNLAB", "--out", str(tmp_path / "o")]
    assert cli.main([*args, "--config", str(tmp_path / "profiles")]) == 1
    err = capsys.readouterr().err
    assert f"  skipped 1 study(ies): {SKIP_STARTED}" in err
    index = json.loads((tmp_path / "o" / INDEX_NAME).read_text(encoding="utf-8"))
    assert index["skipped"] == [
        {"imaging_study": CR_STUDY, "reason": SKIP_STARTED, "source": "file 1"}
    ]
    assert len(index["studies"]) == 1


# -- values from the bundle ----------------------------------------------------------------


def test_codes_priorities_and_text_are_written_safely(tmp_path, hospital):
    def change(b):
        _resource(b, "Patient")["name"][0]["family"] = "ZZZ|PIPE^CARET"
        _resource(b, "ServiceRequest")["priority"] = "urgent"
        _resource(b, "ServiceRequest")["code"]["coding"][0]["system"] = "http://loinc.org"
        _cr(b)["procedureCode"] = []

    bundle = _alpha_with(change)
    run = generate_from_synthea(_write(tmp_path / "in", "b.json", bundle), hospital, seed=42)
    cr, ct = (
        next(s for s in run.index["studies"] if s["imaging_study"] == r)
        for r in (CR_STUDY, CT_STUDY)
    )
    cr_orm = parse_hl7(run.files[cr["order_file"]].decode())
    assert get_field(cr_orm, "OBR-4", component=1) == "IMGCR"
    assert get_field(cr_orm, "OBR-4", component=2) == "CR imaging study"
    assert get_field(cr_orm, "OBR-4", component=3) == "99HUB"
    for name, text in _messages(run):
        assert lint_text(text, _registry(tmp_path)) == [], name
    assert get_field(cr_orm, "PID-5", component=1) == "ZZZ|PIPE^CARET"
    ct_orm = parse_hl7(run.files[ct["order_file"]].decode())
    assert get_field(ct_orm, "OBR-4", component=3) == "LN"
    assert get_field(ct_orm, "OBR-5") == ""


def test_index_holds_no_names_or_birth_dates(hospital):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    text = run.files[INDEX_NAME].decode()
    for value in ("zzztest", "alpha", "qqfake", "charlie", "bravo", "1961", "19610412", ".json"):
        assert value not in text.lower()
    assert ALPHA in text  # the MRN is the join key


def test_the_documented_error_lines(tmp_path, registry, capsys):
    """The two from-synthea rows in docs/cli.md, word for word."""
    config = ["--config", str(tmp_path / "profiles")]
    (tmp_path / "msgs").mkdir()
    (tmp_path / "msgs" / "old.hl7").write_text("x", encoding="utf-8")
    args = ["from-synthea", str(FIXTURES), "--hospital", "SYNLAB", "--out", str(tmp_path / "msgs")]
    assert cli.main([*args, *config]) == 2
    expected = "error: --out is not empty; from-synthea never mixes two runs. Pick a new folder."
    assert expected in capsys.readouterr().err
    path = _write(tmp_path / "in", "a.json", {"resourceType": "Patient"})
    args = ["from-synthea", str(path), "--hospital", "SYNLAB", "--out", str(tmp_path / "new")]
    assert cli.main([*args, *config]) == 2
    assert (
        "error: file 1 is not a FHIR Bundle. Point PATH at output/fhir." in capsys.readouterr().err
    )


# -- inputs Synthea never writes, which must still not crash -----------------------------


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(identifier=5),
        lambda p: p.update(identifier=[{"system": ["x"], "value": "1"}]),
        lambda p: p["identifier"][1].update(type="MR"),
        lambda p: p["identifier"][1]["type"].update(coding=["MR"]),
        lambda p: p.update(name="Bob"),
        lambda p: p.update(name=["Bob"]),
        lambda p: p["name"][0].update(given=5),
        lambda p: p["name"][0].update(family=["X"]),
        lambda p: p.update(gender=1),
    ],
)
def test_a_patient_not_in_fhir_shape_is_a_skip_not_a_crash(tmp_path, hospital, change):
    bundle = _load("qqfake_charlie.json")
    change(_resource(bundle, "Patient"))
    assert _skips(tmp_path, hospital, bundle) == [SKIP_PATIENT_SHAPE]


def test_a_lone_surrogate_is_not_utf8_json(tmp_path, hospital):
    text = (FIXTURES / "qqfake_charlie.json").read_text(encoding="utf-8")
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "c.json").write_bytes(text.replace('"QQFAKE"', r'"QQ\ud800"').encode())
    _usage(folder, hospital, "^file 1 is not UTF-8 JSON.$")


@pytest.mark.parametrize("mrn", [" ", "MRN1 ", " MRN1", "MRN,1", "MRN$1"])
def test_an_mrn_the_listener_would_read_differently_skips_the_patient(tmp_path, hospital, mrn):
    bundle = _load("qqfake_charlie.json")
    _resource(bundle, "Patient")["identifier"][1]["value"] = mrn
    assert _skips(tmp_path, hospital, bundle) == [SKIP_UNSAFE_MRN]


def test_a_repeated_local_hour_is_skipped_only_without_offsets(tmp_path):
    bundle = _load("qqfake_charlie.json")
    _resource(bundle, "ImagingStudy")["started"] = "2026-11-01T05:50:00+00:00"  # 01:50 EDT
    path = _write(tmp_path / "in", "c.json", bundle)
    plain = _registry(tmp_path / "a", offset=False).by_id["SYNLAB"]
    assert [k.reason for k in generate_from_synthea(path, plain, seed=42).skipped] == [
        SKIP_REPEATED_HOUR
    ]
    with_offset = _registry(tmp_path / "b").by_id["SYNLAB"]
    assert generate_from_synthea(path, with_offset, seed=42).skipped == []


def test_a_visits_adt_comes_before_every_order_in_it(tmp_path, hospital):
    bundle = _load("zzztest_alpha.json")
    _resource(bundle, "Encounter", 1)["period"].pop("start")
    later = copy.deepcopy(_ct(bundle))
    later["id"] = "00000000-5e1d-4a00-8000-000000000098"
    later["started"] = "2026-07-19T23:30:00-04:00"
    order = copy.deepcopy(_resource(bundle, "ServiceRequest"))
    order["id"] = "00000000-5e1d-4a00-8000-000000000097"
    order["authoredOn"] = "2026-07-19T22:10:00-04:00"
    later["basedOn"] = [{"reference": f"ServiceRequest/{order['id']}"}]
    bundle["entry"] += [{"resource": later}, {"resource": order}]
    run = generate_from_synthea(_write(tmp_path / "in", "a.json", bundle), hospital, seed=42)
    emer = [s for s in run.index["studies"] if s["imaging_study"] != CR_STUDY]
    assert len(emer) == 2 and emer[0]["adt_file"] == emer[1]["adt_file"]
    for study in emer:
        assert study["adt_file"] < study["order_file"] < study["report_file"]


def test_a_profile_that_refuses_t_or_holds_a_separator_is_refused(tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    text = _profile_text().replace("  tag:", "  allowed_processing_ids: [D]\n  tag:")
    (folder / "lab.yaml").write_text(text, encoding="utf-8")
    hospital = HospitalRegistry.load_dir(folder).by_id["SYNLAB"]
    _usage(FIXTURES, hospital, "does not allow processing id T")
    text = _profile_text().replace("facility_codes: [SYNLAB]", 'facility_codes: ["ST&MARY"]')
    (folder / "lab.yaml").write_text(text, encoding="utf-8")
    hospital = HospitalRegistry.load_dir(folder).by_id["SYNLAB"]
    _usage(FIXTURES, hospital, "holds an HL7 separator; it cannot go into MSH")
    text = _profile_text(version="2.5&1")
    (folder / "lab.yaml").write_text(text, encoding="utf-8")
    hospital = HospitalRegistry.load_dir(folder).by_id["SYNLAB"]
    _usage(FIXTURES, hospital, "holds an HL7 separator; it cannot go into MSH")


def test_a_message_over_the_cap_skips_its_study(monkeypatch, hospital):
    monkeypatch.setattr(synthea, "MAX_MESSAGE_BYTES", 600)
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    assert {k.reason for k in run.skipped} == {SKIP_TOO_LARGE} and len(run.skipped) == 4
    assert not run.counts and json.loads(run.files[INDEX_NAME])["studies"] == []


def test_a_skipped_copy_does_not_make_the_good_copy_a_duplicate(tmp_path, hospital):
    folder = tmp_path / "in"
    bad = _load("qqfake_charlie.json")
    _resource(bad, "ImagingStudy")["started"] = "2026-09-01"
    _write(folder, "a.json", bad)
    _write(folder, "b.json", _load("qqfake_charlie.json"))
    run = generate_from_synthea(folder, hospital, seed=42)
    assert [(k.source, k.reason) for k in run.skipped] == [("file 1", SKIP_STARTED)]
    assert run.counts["ORM^O01"] == 1


def test_out_inside_path_is_refused(tmp_path, registry):
    folder = _bundles(tmp_path, "qqfake_charlie.json")
    with pytest.raises(UsageError, match="is inside"):
        synthea.check_out(folder, folder / "out")
    args = ["from-synthea", str(folder), "--hospital", "SYNLAB", "--out", str(folder / "o")]
    assert cli.main([*args, "--config", str(tmp_path / "profiles")]) == 2
    assert not (folder / "o").exists()


def test_only_lower_case_json_files_are_read_on_every_os(tmp_path, hospital):
    folder = tmp_path / "in"
    folder.mkdir()
    shutil.copy(FIXTURES / "qqfake_charlie.json", folder / "c.JSON")
    _usage(folder, hospital, r"no \*\.json files under")


def test_a_changed_message_gets_a_new_control_id(tmp_path, hospital):
    before = generate_from_synthea(FIXTURES / "qqfake_charlie.json", hospital, seed=42)
    bundle = _load("qqfake_charlie.json")
    _resource(bundle, "Patient")["name"][0]["family"] = "QQOTHER"
    after = generate_from_synthea(_write(tmp_path / "in", "c.json", bundle), hospital, seed=42)
    ids = [{get_field(parse_hl7(t), "MSH-10") for _, t in _messages(r)} for r in (before, after)]
    assert not ids[0] & ids[1]
    assert before.index["studies"][0]["accession"] == after.index["studies"][0]["accession"]


def test_a_study_with_no_id_and_no_full_url_is_skipped(tmp_path, hospital):
    bundle = _load("qqfake_charlie.json")
    _resource(bundle, "ImagingStudy").pop("id")
    assert _skips(tmp_path, hospital, bundle) == [SKIP_NO_ID]


# -- more edge cases: links, MRN systems, ids, caps and the output folder ----------------

OTHER = "00000000-5e1d-4a00-8000-000000000009"
OTHER_MRN_SYSTEM = "urn:test:other-mrn"
SECRET = "ZZZSECRET_Person_Name"


def _direct(tmp_path: Path, registry, run, patients: list[dict]) -> tuple[FakeFhir, list]:
    """Every message of ``run`` through the direct listener, the patients
    loaded first: (server, [(file name, ACK code)])."""
    server = FakeFhir()
    for n, patient in enumerate(patients, start=1):
        server._write("Patient", f"p{n}", {k: v for k, v in patient.items() if k != "id"})
    acks = []
    with HapiClient(BASE, transport=server.transport()) as client:
        handler = DirectHandler(
            registry,
            client,
            RunScope("lab-a", TAG),
            DirectLog(tmp_path / f"d{len(list(tmp_path.glob('d*.db')))}.db"),
            clock=lambda: NOW,
        )
        for name, text in _messages(run):
            ack = handler.handle_bytes(text.encode())
            acks.append((name, ack.split("\rMSA|")[1].split("|")[0]))
    return server, acks


def _dir_link(link: Path, target: Path) -> None:
    """A junction on Windows (no privilege needed), a symlink elsewhere."""
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def _with_other_patient(bundle: dict) -> dict:
    other = copy.deepcopy(_resource(bundle, "Patient"))
    other["id"] = OTHER
    other["identifier"][1]["value"] = OTHER
    other["name"] = [{"family": "QQOTHERPERSON", "given": ["Zed"]}]
    bundle["entry"].append({"fullUrl": f"urn:uuid:{OTHER}", "resource": other})
    return bundle


@pytest.mark.parametrize("kind", ["Encounter", "ServiceRequest", "DiagnosticReport"])
def test_a_linked_resource_of_another_patient_is_never_used(tmp_path, hospital, kind):
    bundle = _with_other_patient(_load("zzztest_alpha.json"))
    if kind == "DiagnosticReport":
        bundle["entry"].append({"resource": _linked_report(conclusion="QQOTHERPERSON read")})
        target = bundle["entry"][-1]["resource"]
    elif kind == "Encounter":
        target = _resource(bundle, "Encounter", 1)  # the CT study's emergency visit
    else:
        target = _resource(bundle, "ServiceRequest")
    target["subject"] = {"reference": f"urn:uuid:{OTHER}"}
    run = generate_from_synthea(_write(tmp_path / "in", "a.json", bundle), hospital, seed=42)
    assert CT_STUDY not in {s["imaging_study"] for s in run.index["studies"]}
    assert [(k.imaging_study, k.reason) for k in run.skipped] == [
        (CT_STUDY, synthea.SKIP_OTHER_PATIENT)
    ]


def test_a_linked_resource_naming_the_same_patient_another_way_is_used(tmp_path, hospital):
    bundle = _load("zzztest_alpha.json")
    _resource(bundle, "ServiceRequest")["subject"] = {"reference": f"Patient/{ALPHA}"}
    run = generate_from_synthea(_write(tmp_path / "in", "a.json", bundle), hospital, seed=42)
    assert run.skipped == [] and len(run.index["studies"]) == 2


def test_the_same_mrn_under_two_systems_is_two_patients(tmp_path):
    registry = _registry(tmp_path, by_authority=f"{{OTHERHOSP: {OTHER_MRN_SYSTEM}}}")
    first = _load("qqfake_charlie.json")
    second = copy.deepcopy(first)
    _resource(second, "Patient")["identifier"][1]["system"] = OTHER_MRN_SYSTEM
    _resource(second, "Patient")["name"][0]["family"] = "QQSECOND"
    folder = tmp_path / "in"
    _write(folder, "a.json", first)
    _write(folder, "b.json", second)
    run = generate_from_synthea(folder, registry.by_id["SYNLAB"], seed=42)
    assert run.skipped == []
    a, b = run.index["studies"]
    assert a["mrn"] == b["mrn"] and a["mrn_system"] != b["mrn_system"]
    assert a["accession"] != b["accession"] and a["visit"] != b["visit"]
    assert run.counts["ADT^A04"] == 2


def test_no_error_or_status_line_names_a_path(tmp_path, registry, capsys, monkeypatch):
    config = ["--hospital", "SYNLAB", "--config", str(tmp_path / "profiles")]
    folder = tmp_path / SECRET
    folder.mkdir()
    full = tmp_path / f"full{SECRET}"
    full.mkdir()
    (full / "x").write_text("x", encoding="utf-8")
    a_file = tmp_path / f"file{SECRET}"
    a_file.write_text("x", encoding="utf-8")
    empty = tmp_path / f"empty{SECRET}"
    empty.mkdir()
    refused = [
        [str(folder / f"{SECRET}.json"), "--out", str(tmp_path / f"o1{SECRET}")],  # missing
        [str(folder), "--out", str(tmp_path / f"o2{SECRET}")],  # an empty folder
        [str(FIXTURES), "--out", str(full)],  # --out not empty
        [str(FIXTURES), "--out", str(a_file)],  # --out a file
        [str(FIXTURES), "--out", str(empty)],  # --out an existing empty folder
    ]
    for args in refused:
        assert cli.main(["from-synthea", *args, *config]) == 2, args
    shutil.copy(FIXTURES / "hospitalInformation1700000000000.json", folder / f"{SECRET}.json")
    for args in (
        [str(folder), "--out", str(tmp_path / f"o3{SECRET}")],  # no ImagingStudy
        [str(folder), "--out", str(folder / "out")],  # --out inside PATH
    ):
        assert cli.main(["from-synthea", *args, *config]) == 2, args
    monkeypatch.setattr(synthea, "MAX_FILES", 0)
    assert cli.main(["from-synthea", str(folder), "--out", str(tmp_path / "o4"), *config]) == 2
    monkeypatch.setattr(synthea, "MAX_FILES", 20_000)
    good = [str(FIXTURES / "qqfake_charlie.json"), "--out", str(tmp_path / f"good{SECRET}")]
    assert cli.main(["from-synthea", *good, *config]) == 0
    captured = capsys.readouterr()
    assert captured.err.count("error: ") == 8
    assert SECRET not in captured.out + captured.err


def test_the_index_names_no_full_url_or_free_text_id(tmp_path, hospital):
    bundle = _load("zzztest_alpha.json")
    cr, ct = _cr(bundle), _ct(bundle)
    cr.pop("id")
    next(e for e in bundle["entry"] if e["resource"] is cr)["fullUrl"] = f"file:///s/{SECRET}.json"
    ct["id"] = f"{SECRET}-study"
    bad = copy.deepcopy(ct)
    bad["id"] = f"{SECRET}-bad"
    bad["started"] = "2026-01-01"
    bundle["entry"].append({"resource": bad})
    run = generate_from_synthea(_write(tmp_path / "in", "a.json", bundle), hospital, seed=42)
    assert len(run.index["studies"]) == 2 and len(run.index["skipped"]) == 1
    assert SECRET not in run.files[INDEX_NAME].decode()
    labels = [s["imaging_study"] for s in run.index["studies"]]
    labels += [k["imaging_study"] for k in run.index["skipped"]]
    assert len(set(labels)) == 3


@pytest.mark.parametrize("field", ALLOWED_ACCESSION_FIELDS)
def test_orc_and_obr_carry_the_same_placer_and_filler_numbers(tmp_path, field):
    registry = _registry(tmp_path, accession_field=field)
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    for study in run.index["studies"]:
        for kind in ("order_file", "report_file"):
            msg = parse_hl7(run.files[study[kind]].decode())
            assert get_field(msg, "ORC-2") == get_field(msg, "OBR-2") != ""
            assert get_field(msg, "ORC-3") == get_field(msg, "OBR-3")
            assert get_field(msg, field) == study["accession"]


@pytest.mark.parametrize("field", ALLOWED_ACCESSION_FIELDS)
def test_every_accession_field_gets_aa_from_the_direct_listener(tmp_path, field):
    registry = _registry(tmp_path, accession_field=field)
    run = generate_from_synthea(FIXTURES, registry.by_id["SYNLAB"], seed=42)
    names = ("zzztest_alpha.json", "zzzsample_bravo.json", "qqfake_charlie.json")
    server, acks = _direct(tmp_path, registry, run, [_resource(_load(n), "Patient") for n in names])
    assert {code for _, code in acks} == {"AA"}
    assert len(server.all("DiagnosticReport")) == 4


def test_a_copy_too_large_to_write_does_not_block_a_good_copy(tmp_path, hospital):
    folder = tmp_path / "in"
    big = _load("qqfake_charlie.json")
    _resource(big, "Patient")["name"][0]["family"] = "QQ" + "X" * (70 * 1024)
    _write(folder, "a.json", big)
    _write(folder, "b.json", _load("qqfake_charlie.json"))
    run = generate_from_synthea(folder, hospital, seed=42)
    assert run.counts["ORM^O01"] == 1
    assert [(k.source, k.reason) for k in run.skipped] == [("file 1", SKIP_TOO_LARGE)]


def test_a_copy_in_a_repeated_hour_does_not_block_a_good_copy(tmp_path):
    plain = _registry(tmp_path, offset=False).by_id["SYNLAB"]
    folder = tmp_path / "in"
    early = _load("qqfake_charlie.json")
    _resource(early, "ImagingStudy")["started"] = "2026-11-01T05:50:00+00:00"  # 01:50 EDT
    _write(folder, "a.json", early)
    _write(folder, "b.json", _load("qqfake_charlie.json"))
    run = generate_from_synthea(folder, plain, seed=42)
    assert run.counts["ORM^O01"] == 1
    assert [(k.source, k.reason) for k in run.skipped] == [("file 1", SKIP_REPEATED_HOUR)]


@pytest.mark.parametrize(
    ("identifiers", "uid"),
    [
        (
            [
                {"system": "urn:local:acc", "value": "98765"},
                {"system": "urn:ietf:rfc:3986", "value": "urn:oid:1.2.840.99.1"},
            ],
            "1.2.840.99.1",
        ),
        ([{"system": "urn:dicom:uid", "value": "urn:oid:1.2.840.99.2"}], "1.2.840.99.2"),
        ([{"system": "urn:dicom:uid", "value": "1.2.840.99.3"}], "1.2.840.99.3"),
        ([{"system": "urn:dicom:uid", "value": "urn:dicom:uid:1.2.840.99.4"}], "1.2.840.99.4"),
        ([{"system": "urn:local:acc", "value": "98765"}], None),
        ([{"system": "urn:local:acc", "value": "1.2.840.5"}], None),
        ([{"system": "urn:dicom:uid", "value": "98765"}], None),
        ([{"system": "urn:dicom:uid", "value": "1.02.3"}], None),
        ([{"system": "urn:dicom:uid", "value": "1." + "2" * 70}], None),
    ],
)
def test_the_study_uid_comes_only_from_a_dicom_uid_identifier(tmp_path, hospital, identifiers, uid):
    bundle = _load("qqfake_charlie.json")
    _resource(bundle, "ImagingStudy")["identifier"] = identifiers
    run = generate_from_synthea(_write(tmp_path / "in", "c.json", bundle), hospital, seed=42)
    assert run.index["studies"][0]["study_uid"] == uid


@pytest.mark.parametrize("change", [{"facility": "OTHERLAB"}, {"version": "2.4"}])
def test_msh10_changes_with_msh4_and_msh12(tmp_path, change):
    a = generate_from_synthea(FIXTURES, _registry(tmp_path / "a").by_id["SYNLAB"], seed=42)
    b = generate_from_synthea(
        FIXTURES, _registry(tmp_path / "b", **change).by_id["SYNLAB"], seed=42
    )
    ids = [{get_field(parse_hl7(t), "MSH-10") for _, t in _messages(r)} for r in (a, b)]
    assert not ids[0] & ids[1]


@pytest.mark.parametrize(
    ("authority", "refused"),
    [
        ("ST&MARY", True),
        (" ST", True),
        ("ST ", True),
        ("ST^MARY", False),
        ("ST~MARY", False),
        ("ST\\MARY", False),
        ("ST|MARY", False),
        ("ST MARY", False),
    ],
)
def test_an_mrn_authority_the_direct_listener_cannot_read_back_is_refused(
    tmp_path, authority, refused
):
    registry = _registry(tmp_path, by_authority=f"{{{json.dumps(authority)}: {OTHER_MRN_SYSTEM}}}")
    bundle = _load("qqfake_charlie.json")
    patient = _resource(bundle, "Patient")
    patient["identifier"][1]["system"] = OTHER_MRN_SYSTEM
    path = _write(tmp_path / "in", "c.json", bundle)
    if refused:
        with pytest.raises(UsageError, match="mrn_by_authority"):
            generate_from_synthea(path, registry.by_id["SYNLAB"], seed=42)
        return
    run = generate_from_synthea(path, registry.by_id["SYNLAB"], seed=42)
    _, acks = _direct(tmp_path, registry, run, [patient])
    assert {code for _, code in acks} == {"AA"}, acks


@pytest.mark.parametrize(("lines", "written"), [(499, True), (500, False)])
def test_the_report_line_cap_counts_the_synthetic_footer(tmp_path, hospital, lines, written):
    bundle = _load("zzztest_alpha.json")
    bundle["entry"].append({"resource": _linked_report(conclusion="x\n" * lines)})
    run = generate_from_synthea(_write(tmp_path / "in", "a.json", bundle), hospital, seed=42)
    study = next((s for s in run.index["studies"] if s["imaging_study"] == CT_STUDY), None)
    if study is not None:
        oru = run.files[study["report_file"]].decode().split("\r")
        assert sum(s.startswith("OBX|") for s in oru) <= synthea.MAX_REPORT_LINES
    assert (study is not None) is written
    assert [k.reason for k in run.skipped] == ([] if written else [SKIP_REPORT_TEXT])


def test_a_local_offset_with_seconds_is_never_written_shifted(tmp_path):
    """Before 1911 Paris kept UTC+00:09:21. HL7 offsets have no seconds."""
    bundle = _load("qqfake_charlie.json")
    started = "1905-06-01T10:00:00+00:00"
    _resource(bundle, "ImagingStudy")["started"] = started
    path = _write(tmp_path / "in", "c.json", bundle)
    for n, offset in enumerate((True, False)):
        paris = _registry(tmp_path / str(n), timezone="Europe/Paris", offset=offset)
        hospital = paris.by_id["SYNLAB"]
        run = generate_from_synthea(path, hospital, seed=42)
        for study in run.index["studies"]:
            oru = parse_hl7(run.files[study["report_file"]].decode())
            exam = parse_hl7_ts(get_field(oru, "OBR-7"), default_tz=hospital.tzinfo)
            assert exam == datetime.fromisoformat(started)
        expected = [synthea.SKIP_OFFSET_SECONDS] if offset else []
        assert [k.reason for k in run.skipped] == expected


def test_a_failed_write_leaves_no_output_behind(tmp_path, hospital, monkeypatch):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    real_write = OwnedFiles.write_bytes
    calls = []

    def failing_write(self, path, data, **kwargs):
        calls.append(path)
        if len(calls) == 3:
            raise OSError(28, "No space left on device")
        return real_write(self, path, data, **kwargs)

    monkeypatch.setattr(OwnedFiles, "write_bytes", failing_write)
    parent = tmp_path / "parent"
    with pytest.raises(OSError):
        write_run(run, parent / "out")
    assert list(parent.iterdir()) == []  # no --out, no staged folder


def test_an_out_created_by_another_writer_meanwhile_is_left_alone(tmp_path, hospital, monkeypatch):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    out = tmp_path / "out"
    real_write = OwnedFiles.write_bytes

    def racing_write(self, path, data, **kwargs):
        real_write(self, path, data, **kwargs)
        if path.name == INDEX_NAME:  # the last file: another writer makes --out now
            out.mkdir()
            (out / "00001_adt_a04.hl7").write_text("theirs", encoding="utf-8")

    monkeypatch.setattr(OwnedFiles, "write_bytes", racing_write)
    with pytest.raises(UsageError, match=r"^--out was created while writing"):
        write_run(run, out)
    assert [p.name for p in out.iterdir()] == ["00001_adt_a04.hl7"]
    assert (out / "00001_adt_a04.hl7").read_text(encoding="utf-8") == "theirs"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "profiles"]


def test_an_existing_empty_out_is_refused_before_any_bundle_is_read(tmp_path, registry, capsys):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.json").write_bytes(b"{")
    out = tmp_path / "out"
    out.mkdir()
    config = ["--hospital", "SYNLAB", "--config", str(tmp_path / "profiles")]
    assert cli.main(["from-synthea", str(folder), "--out", str(out), *config]) == 2
    assert (
        "error: --out already exists; from-synthea writes a new folder" in capsys.readouterr().err
    )
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("where", ["mkdtemp", "identity"])
def test_a_folder_that_cannot_be_staged_is_a_usage_error_naming_no_path(
    tmp_path, hospital, monkeypatch, where
):
    run = generate_from_synthea(FIXTURES, hospital, seed=42)
    out = tmp_path / SECRET / "out"
    if where == "mkdtemp":

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied", str(out.parent))

        monkeypatch.setattr(synthea.tempfile, "mkdtemp", refuse)
    else:

        def refuse(path):
            raise DemoDirError(f"{path} is not a real folder")

        monkeypatch.setattr(demofiles, "dir_identity", refuse)
    with pytest.raises(UsageError) as caught:
        write_run(run, out)
    assert SECRET not in f"{caught.value} {caught.value.hint}"
    assert not out.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps the owner-only ACL mkdtemp sets")
def test_the_output_gets_the_umask_modes_not_mkdtemps(tmp_path, hospital):
    # Under 022, not the runner's own umask: with 077, mkdtemp's 0700 and the
    # 0600 files already match it, so the test could not tell.
    out = tmp_path / "out"
    previous = os.umask(0o022)
    try:
        write_run(generate_from_synthea(FIXTURES, hospital, seed=42), out)
    finally:
        os.umask(previous)
    assert out.stat().st_mode & 0o777 == 0o755
    assert {p.stat().st_mode & 0o777 for p in out.iterdir()} == {0o644}


def test_a_link_at_out_is_refused(tmp_path, registry, capsys):
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "missing", target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    config = ["--hospital", "SYNLAB", "--config", str(tmp_path / "profiles")]
    assert cli.main(["from-synthea", str(FIXTURES), "--out", str(link), *config]) == 2
    assert "error: --out is a link" in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()


def test_a_junction_or_link_to_an_empty_folder_at_out_is_refused(tmp_path, registry, capsys):
    (tmp_path / "target").mkdir()
    _dir_link(tmp_path / "via", tmp_path / "target")
    config = ["--hospital", "SYNLAB", "--config", str(tmp_path / "profiles")]
    assert cli.main(["from-synthea", str(FIXTURES), "--out", str(tmp_path / "via"), *config]) == 2
    assert "error: --out is a link" in capsys.readouterr().err
    assert list((tmp_path / "target").iterdir()) == []


def test_a_full_out_is_refused_before_any_bundle_is_read(tmp_path, registry, capsys):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.json").write_bytes(b"{")
    out = tmp_path / "out"
    out.mkdir()
    (out / "old.hl7").write_text("x", encoding="utf-8")
    config = ["--hospital", "SYNLAB", "--config", str(tmp_path / "profiles")]
    assert cli.main(["from-synthea", str(folder), "--out", str(out), *config]) == 2
    assert "error: --out is not empty" in capsys.readouterr().err


def test_a_linked_folder_inside_path_is_not_read(tmp_path, hospital):
    folder = _bundles(tmp_path, "qqfake_charlie.json")
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(FIXTURES / "zzztest_alpha.json", outside / "a.json")
    _dir_link(folder / "linked", outside)
    run = generate_from_synthea(folder, hospital, seed=42)
    assert run.bundles == 1
    write_run(run, outside / "out")  # check_out passes: outside is not inside PATH
    assert generate_from_synthea(folder, hospital, seed=42).bundles == 1


def test_out_through_a_link_into_path_or_at_path_is_refused(tmp_path):
    folder = _bundles(tmp_path, "qqfake_charlie.json")
    (folder / "sub").mkdir()
    _dir_link(tmp_path / "via", folder / "sub")
    for out in (tmp_path / "via" / "out", folder, folder / "x" / ".." / "y", folder / "." / "z"):
        with pytest.raises(UsageError, match=r"^--out is inside PATH"):
            synthea.check_out(folder, out)
