"""PID-7 birth time: Patient.birthDate keeps the date at the precision sent,
and a PID-7 with minutes or finer adds the patient-birthTime extension on
birthDate (IG ConceptMap-segment-pid-to-patient). The post merge keeps or
clears that time together with birthDate, the store path reads PID-7 the
same way, and from-fhir writes the time back into PID-7."""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import MappingError, TimestampError, UsageError
from hl7bridge.fhir import sync_to_fhir
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.post import merge_resource, post_mapped
from hl7bridge.fromfhir import Renderer, pick_mrn
from hl7bridge.hl7time import BirthValue, parse_birth
from hl7bridge.ingest import IngestPipeline
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.store import connect
from hl7bridge.v2fhir import bundle_json, message_to_bundle

from ..fakefhir import FakeFhir
from .test_direct import NOW, _patient

BASE = "http://fhir.test/fhir"
SAMPLE = (SAMPLES_DIR / "07_hospc_adt_a04_offsets.hl7").read_bytes().decode()
MRN_KEY = "urn:hl7bridge:hospc:mrn|SC1001"
BIRTH_TIME = "http://hl7.org/fhir/StructureDefinition/patient-birthTime"
FOREIGN = {"url": "urn:example:someone-else", "valueString": "kept"}


@pytest.fixture(scope="module")
def registry() -> HospitalRegistry:
    return HospitalRegistry.load_dir(BUNDLED_PROFILES)


INVALID = [
    "1990062025",  # hour 25
    "199006200360",  # minute 60
    "19900620030460",  # second 60
    "199006200304.5",  # a fraction needs seconds
    "19900620.5",
    "19900620030405.12345",  # HL7 allows four fraction digits
    "1990062003045",  # odd length
    "19900620030405+1500",  # outside the offsets FHIR allows
    "19900620030405+0099",
    "1990+2500",  # an impossible offset on a year or a year and month
    "199006+0099",
    "19810229",  # not a leap year
    "19000229",
    "198002301200",
    "198013",  # month 13
    "199000",
    "0000",  # FHIR has no year 0
    "banana",
    "19",
    "\u0661\u0669\u0669\u0660",  # a year in Arabic-Indic digits
    "\uff11\uff19\uff19\uff10\uff10\uff16",  # full-width digits
    "19900620030405.\u0661",  # a non-ASCII fraction digit
    '""',  # the HL7 null is not a clear of birthDate (unchanged)
]


def _adt(pid7: str, *, event: str = "A04", msh7: str = "20261007080000-0400") -> str:
    text = SAMPLE.replace("|19900620|", f"|{pid7}|").replace("20261007080000-0400", msh7)
    return text.replace("ADT^A04", f"ADT^{event}").replace("EVN|A04", f"EVN|{event}")


def _patient_json(text: str, registry: HospitalRegistry) -> dict:
    bundle = bundle_json(message_to_bundle(text, registry))
    patients = [
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    ]
    assert len(patients) == 1
    return patients[0]


def _birth_time(patient: dict) -> list:
    return [x for x in patient.get("_birthDate", {}).get("extension", []) if x["url"] == BIRTH_TIME]


def _ext(value: str) -> dict:
    return {"url": BIRTH_TIME, "valueDateTime": value}


# -- v2 to FHIR -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pid7", "birth_date"),
    [
        ("19900620", "1990-06-20"),  # date only
        ("1990062003", "1990-06-20"),  # an hour alone is not a FHIR time (needs minutes)
        ("19900620+0500", "1990-06-20"),  # a date with an offset is still a date
        ("1990", "1990"),  # year only: precision kept
        ("199006", "1990-06"),  # year and month: precision kept
        ("1990+0000", "1990"),
        ("19800229", "1980-02-29"),  # leap day
        ("20000229", "2000-02-29"),
    ],
)
def test_birth_date_without_a_time_has_no_extension(registry, pid7, birth_date):
    patient = _patient_json(_adt(pid7), registry)
    assert patient["birthDate"] == birth_date
    assert "_birthDate" not in patient


@pytest.mark.parametrize(
    ("pid7", "msh7", "birth_time"),
    [
        # Minutes: FHIR needs seconds, zero-filled. No offset: MSH-7's wins.
        ("199006200304", "20261007080000-0400", "1990-06-20T03:04:00-04:00"),
        ("19900620030405", "20261007080000-0400", "1990-06-20T03:04:05-04:00"),
        ("19900620030405.1", "20261007080000-0400", "1990-06-20T03:04:05.100000-04:00"),
        ("19900620030405.1234", "20261007080000-0400", "1990-06-20T03:04:05.123400-04:00"),
        # The value's own offset wins over MSH-7's.
        ("19900620030405+0530", "20261007080000-0400", "1990-06-20T03:04:05+05:30"),
        ("19900620030405-0000", "20261007080000-0400", "1990-06-20T03:04:05Z"),
        # No offset anywhere: the hospital's zone (America/New_York) on that
        # date, daylight time in June and standard time in January.
        ("19900620030405", "20261007080000", "1990-06-20T03:04:05-04:00"),
        ("19900120030405", "20261007080000", "1990-01-20T03:04:05-05:00"),
        # The wall time is kept, not moved to UTC, so the date stays birthDate's.
        ("199006202330-0500", "20261007080000-0400", "1990-06-20T23:30:00-05:00"),
        ("198002291230", "20261007080000-0400", "1980-02-29T12:30:00-04:00"),
        # In the hospital zone a wall time that does not exist (spring forward)
        # or happens twice (fall back) takes fold=0, as every other timestamp.
        ("199004010230", "20261007080000", "1990-04-01T02:30:00-05:00"),
        ("199010280130", "20261007080000", "1990-10-28T01:30:00-04:00"),
        # A time of all zeros is a time (the IG's condition is the length).
        ("199006200000", "20261007080000-0400", "1990-06-20T00:00:00-04:00"),
    ],
)
def test_birth_time_extension(registry, pid7, msh7, birth_time):
    patient = _patient_json(_adt(pid7, msh7=msh7), registry)
    assert patient["birthDate"] == birth_time[:10]
    assert patient["_birthDate"] == {"extension": [_ext(birth_time)]}


@pytest.mark.parametrize("pid7", INVALID)
def test_invalid_birth_values_are_refused(registry, pid7):
    with pytest.raises(MappingError) as caught:
        message_to_bundle(_adt(pid7), registry)
    assert str(caught.value) == "PID-7: not a valid HL7 date"
    assert pid7 not in str(caught.value)


def test_a_local_mean_time_offset_is_refused(registry):
    # New York before standard time (1883) was 4:56:02 behind UTC; FHIR
    # offsets are whole minutes, so the time cannot be written.
    with pytest.raises(MappingError, match="PID-7: not a valid HL7 date"):
        message_to_bundle(_adt("188001020304", msh7="20261007080000"), registry)
    assert _patient_json(_adt("18800102", msh7="20261007080000"), registry)["birthDate"] == (
        "1880-01-02"
    )
    # The same without relying on the zone database's pre-1883 data.
    lmt = timezone(-timedelta(hours=4, minutes=56, seconds=2))
    with pytest.raises(TimestampError):
        parse_birth("188001020304", default_tz=lmt)
    assert parse_birth("18800102", default_tz=lmt) == BirthValue("1880-01-02", None)


# -- store path -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pid7", "stored"),
    [("1990", "1990"), ("199006", "1990-06"), ("199006200304-0500", "1990-06-20")],
)
def test_store_keeps_the_birth_date_precision(tmp_path, registry, pid7, stored):
    conn = connect(tmp_path / "s.db")
    ack = IngestPipeline(registry, conn).handle_bytes(_adt(pid7).encode())
    assert "MSA|AA|" in ack
    assert conn.execute("SELECT birth_date FROM patients").fetchone()[0] == stored
    conn.close()


@pytest.mark.parametrize(
    ("pid7", "msh7"),
    [
        *((pid7, "20261007080000-0400") for pid7 in INVALID),
        # local mean time: no offset anywhere, New York before 1883
        ("188001020304", "20261007080000"),
    ],
)
def test_store_refuses_what_the_mapping_refuses(tmp_path, registry, pid7, msh7):
    conn = connect(tmp_path / "s.db")
    ack = IngestPipeline(registry, conn).handle_bytes(_adt(pid7, msh7=msh7).encode())
    assert "MSA|AE|" in ack
    msa = next(s for s in ack.splitlines() if s.startswith("MSA|"))
    assert msa.endswith("|bad_timestamp: PID-7")
    assert pid7 not in msa
    conn.close()


# -- merge ------------------------------------------------------------------------------


def _current(**extra) -> dict:
    return {
        "resourceType": "Patient",
        "id": "p1",
        "meta": {"versionId": "1"},
        "identifier": [{"system": "urn:x", "value": "1"}],
        "birthDate": "1990-06-20",
        **extra,
    }


def test_merge_clears_a_stale_birth_time_when_the_message_has_only_a_date():
    current = _current(_birthDate={"extension": [_ext("1990-06-20T03:04:05-04:00")]})
    new = {"resourceType": "Patient", "birthDate": "1990-06-20"}
    merged = merge_resource(current, new)
    assert merged["birthDate"] == "1990-06-20"
    assert "_birthDate" not in merged


def test_merge_replaces_every_birth_time_entry():
    old = [_ext("1990-06-20T03:04:05-04:00"), FOREIGN, _ext("1990-06-20T04:05:06-04:00")]
    current = _current(_birthDate={"extension": old})
    merged = merge_resource(current, {"resourceType": "Patient", "birthDate": "1990-06-20"})
    assert merged["_birthDate"] == {"extension": [FOREIGN]}
    new = {
        "resourceType": "Patient",
        "birthDate": "1990-06-20",
        "_birthDate": {"extension": [_ext("1990-06-20T09:00:00-04:00")]},
    }
    merged = merge_resource(_current(_birthDate={"extension": old}), new)
    assert merged["_birthDate"] == {"extension": [FOREIGN, _ext("1990-06-20T09:00:00-04:00")]}


def test_merge_keeps_other_extensions_on_birth_date():
    current = _current(
        _birthDate={"id": "b", "extension": [FOREIGN, _ext("1990-06-20T03:04:05-04:00")]}
    )
    new = {"resourceType": "Patient", "birthDate": "1990-06-21"}
    merged = merge_resource(current, new)
    assert merged["birthDate"] == "1990-06-21"
    assert merged["_birthDate"] == {"id": "b", "extension": [FOREIGN]}


def test_merge_adds_or_replaces_the_birth_time():
    new = {
        "resourceType": "Patient",
        "birthDate": "1990-06-20",
        "_birthDate": {"extension": [_ext("1990-06-20T09:00:00-04:00")]},
    }
    for current in (
        _current(),
        _current(_birthDate={"extension": [_ext("1990-06-20T03:04:05-04:00")]}),
    ):
        merged = merge_resource(current, new)
        assert merged["_birthDate"] == {"extension": [_ext("1990-06-20T09:00:00-04:00")]}
        assert merge_resource(merged, new) == merged  # a second pass changes nothing
    merged = merge_resource(_current(_birthDate={"extension": [FOREIGN]}), new)
    assert merged["_birthDate"] == {"extension": [FOREIGN, _ext("1990-06-20T09:00:00-04:00")]}


def test_merge_keeps_birth_date_and_time_when_the_message_has_no_pid7():
    current = _current(_birthDate={"extension": [_ext("1990-06-20T03:04:05-04:00")]})
    merged = merge_resource(current, {"resourceType": "Patient", "gender": "female"})
    assert merged["birthDate"] == "1990-06-20"
    assert merged["_birthDate"] == current["_birthDate"]


def test_merge_clear_takes_the_birth_time_with_birth_date():
    current = _current(_birthDate={"extension": [FOREIGN, _ext("1990-06-20T03:04:05-04:00")]})
    new = {"resourceType": "Patient", "gender": "female"}
    merged = merge_resource(current, new, clear=frozenset({"birthDate"}))
    assert "birthDate" not in merged
    assert merged["_birthDate"] == {"extension": [FOREIGN]}


def test_merge_replaces_a_birth_date_element_that_is_not_an_object():
    new = {
        "resourceType": "Patient",
        "birthDate": "1990-06-20",
        "_birthDate": {"extension": [_ext("1990-06-20T09:00:00-04:00")]},
    }
    for junk in ("x", {"extension": "x"}):
        merged = merge_resource(_current(_birthDate=junk), new)
        assert merged["_birthDate"] == {"extension": [_ext("1990-06-20T09:00:00-04:00")]}
    # An entry that is not an extension is not the mapper's: it stays.
    merged = merge_resource(_current(_birthDate={"extension": ["x"]}), new)
    assert merged["_birthDate"] == {"extension": ["x", _ext("1990-06-20T09:00:00-04:00")]}


def _post(server: FakeFhir, registry, *texts: str) -> None:
    with HapiClient(BASE, transport=server.transport()) as client:
        for text in texts:
            post_mapped(client, message_to_bundle(text, registry))


def test_post_a08_with_a_date_only_leaves_no_stale_time(registry):
    server = FakeFhir()
    _post(server, registry, _adt("19900620030405"))
    assert _birth_time(server.by_key("Patient", MRN_KEY)) == [_ext("1990-06-20T03:04:05-04:00")]
    _post(server, registry, _adt("", event="A08"))  # PID-7 left out: kept
    assert _birth_time(server.by_key("Patient", MRN_KEY)) == [_ext("1990-06-20T03:04:05-04:00")]
    _post(server, registry, _adt("19900620", event="A08"))
    patient = server.by_key("Patient", MRN_KEY)
    assert patient["birthDate"] == "1990-06-20"
    assert "_birthDate" not in patient


def _sync(conn, registry, server: FakeFhir) -> None:
    with HapiClient(BASE, transport=server.transport()) as client:
        report = sync_to_fhir(conn, registry, client)
    assert not report.skipped
    assert report.updated["Patient"] == 1


@pytest.mark.parametrize(
    ("first", "stored", "want"),
    [
        # An existing Patient with the extension, a stored message with a date only.
        ("19900620030405", "19900620", []),
        # An existing Patient without it, a stored message with a time.
        ("19900620", "199006200304", [_ext("1990-06-20T03:04:00-04:00")]),
    ],
)
def test_sync_onto_an_existing_patient(tmp_path, registry, first, stored, want):
    server = FakeFhir()
    _post(server, registry, _adt(first))
    conn = connect(tmp_path / "s.db")
    IngestPipeline(registry, conn).handle_bytes(_adt(stored, event="A08").encode())
    _sync(conn, registry, server)
    conn.close()
    assert _birth_time(server.by_key("Patient", MRN_KEY)) == want
    direct = FakeFhir()
    _post(direct, registry, _adt(first), _adt(stored, event="A08"))
    synced = server.by_key("Patient", MRN_KEY)
    posted = direct.by_key("Patient", MRN_KEY)
    for element in ("birthDate", "_birthDate"):
        assert synced.get(element) == posted.get(element), element


def test_sync_skips_a_stored_pid7_that_is_now_refused(tmp_path, registry):
    # Stored by an older release, which kept the date and dropped the rest.
    conn = connect(tmp_path / "s.db")
    assert "MSA|AA|" in IngestPipeline(registry, conn).handle_bytes(_adt("19900620").encode())
    conn.execute("UPDATE messages SET raw = replace(raw, '|19900620|', '|19900620030405+1500|')")
    conn.commit()
    server = FakeFhir()
    with HapiClient(BASE, transport=server.transport()) as client:
        report = sync_to_fhir(conn, registry, client)
    conn.close()
    assert [(kind, reason) for kind, _, reason in report.skipped] == [
        ("ADT^A04", "PID-7: not a valid HL7 date")
    ]
    assert not report.created and not report.updated
    assert server.by_identifier("Patient", *MRN_KEY.split("|")) == []


# -- from-fhir --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lab(tmp_path_factory) -> HospitalRegistry:
    from .test_direct import PROFILE

    folder = tmp_path_factory.mktemp("profiles")
    (folder / "lab.yaml").write_text(PROFILE, encoding="utf-8")
    return HospitalRegistry.load_dir(folder)


def _pid7(hospital, patient: dict) -> str:
    return Renderer(hospital, NOW).pid(patient, pick_mrn(patient)).split("|")[7]


@pytest.mark.parametrize(
    ("value", "pid7"),
    [
        ("1980-01-01T03:04:05-05:00", "19800101030405-0500"),
        ("1980-01-01T03:04:05Z", "19800101030405+0000"),
        ("1980-01-01T03:04:05+00:00", "19800101030405+0000"),
        ("1980-01-01T03:04:05.100000+05:30", "19800101030405.1+0530"),
        ("1980-01-01T03:04:05.1234-05:00", "19800101030405.1234-0500"),
        ("1980-01-01T03:04:00-05:00", "19800101030400-0500"),
        ("1980-01-01T03:04:05.1000000000Z", "19800101030405.1+0000"),
    ],
)
def test_from_fhir_writes_the_birth_time(lab, value, pid7):
    patient = _patient(_birthDate={"extension": [FOREIGN, _ext(value)]})
    assert _pid7(lab.by_id["LAB"], patient) == pid7


def test_from_fhir_writes_the_offset_even_when_the_profile_omits_offsets(registry):
    hospa = registry.by_id["HOSPA"]
    assert not hospa.timestamps_include_offset
    patient = _patient(_birthDate={"extension": [_ext("1980-01-01T03:04:05-08:00")]})
    patient["identifier"][0]["system"] = hospa.systems.mrn
    assert _pid7(hospa, patient) == "19800101030405-0800"


@pytest.mark.parametrize(
    ("birth_date", "pid7"), [("1980-01-01", "19800101"), ("1980", "1980"), ("1980-05", "198005")]
)
def test_from_fhir_without_the_extension_writes_the_date(lab, birth_date, pid7):
    hospital = lab.by_id["LAB"]
    assert _pid7(hospital, _patient(birthDate=birth_date)) == pid7
    foreign = _patient(birthDate=birth_date, _birthDate={"extension": [FOREIGN]})
    assert _pid7(hospital, foreign) == pid7


@pytest.mark.parametrize(
    "extra",
    [
        {"_birthDate": {"extension": [_ext("1980-01-02T03:04:05-05:00")]}},  # other date
        {"_birthDate": {"extension": [_ext("1980-01-02T03:30:00Z")]}},  # a UTC next day
        {"birthDate": "1980", "_birthDate": {"extension": [_ext("1980-01-01T03:04:05Z")]}},
        {"birthDate": None, "_birthDate": {"extension": [_ext("1980-01-01T03:04:05Z")]}},
        {"_birthDate": {"extension": [_ext("1980-01-01")]}},  # no time
        {"_birthDate": {"extension": [_ext("1980-01-01T03:04:05")]}},  # no offset
        {"_birthDate": {"extension": [_ext("1980-01-01T03:04:05.12345Z")]}},
        {"_birthDate": {"extension": [_ext("1980-01-01T03:04:05Z\rOBX|9")]}},
        {"_birthDate": {"extension": [{"url": BIRTH_TIME, "valueDateTime": 7}]}},
        {"_birthDate": {"extension": [{"url": BIRTH_TIME}]}},
        {"_birthDate": {"extension": [_ext("1980-01-01T03:04:05Z"), _ext("1980-01-01T03:04:06Z")]}},
        {"_birthDate": "1980-01-01T03:04:05Z"},
        {"_birthDate": {"extension": "nope"}},
    ],
)
def test_from_fhir_refuses_a_birth_time_it_cannot_write(lab, extra):
    patient = _patient(**extra)
    if patient.get("birthDate") is None:
        patient.pop("birthDate")
    with pytest.raises(UsageError, match="birth"):
        _pid7(lab.by_id["LAB"], patient)


@pytest.mark.parametrize(
    ("value", "mapped"),
    [
        ("1980-01-01T03:04:05-05:00", "1980-01-01T03:04:05-05:00"),
        ("1980-01-01T23:30:00.5+14:00", "1980-01-01T23:30:00.500000+14:00"),
        ("1980-01-01T00:00:00+00:00", "1980-01-01T00:00:00Z"),
    ],
)
def test_birth_time_round_trips_through_from_fhir(registry, value, mapped):
    hospc = registry.by_id["HOSPC"]
    patient = _patient(_birthDate={"extension": [_ext(value)]})
    patient["identifier"][0]["system"] = hospc.systems.mrn
    pid = Renderer(hospc, NOW).pid(patient, pick_mrn(patient))
    old_pid = next(s for s in SAMPLE.splitlines() if s.startswith("PID|"))
    back = _patient_json(SAMPLE.replace(old_pid, pid), registry)
    assert back["birthDate"] == "1980-01-01"
    assert _birth_time(back) == [_ext(mapped)]
    # And once more: the mapped value renders the same PID-7 again.
    again = _patient(_birthDate={"extension": [_ext(mapped)]})
    again["identifier"][0]["system"] = hospc.systems.mrn
    assert Renderer(hospc, NOW).pid(again, pick_mrn(again)) == pid
