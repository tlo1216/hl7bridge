"""fhir reset, from-fhir, mapping hooks, the new profile keys and the CLI
wiring of direct mode."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from hl7bridge import cli
from hl7bridge.config import HospitalRegistry, load_hospital_config
from hl7bridge.errors import ConfigError, MappingError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.reset import execute_reset, plan_reset
from hl7bridge.fhir.runscope import RunScope
from hl7bridge.fromfhir import Renderer, escape, pick_mrn, report_message
from hl7bridge.profiles import BUNDLED_PROFILES
from hl7bridge.v2fhir import hooks, message_to_bundle

from ..fakefhir import FakeFhir
from .test_direct import LAB_TAG, NOW, PROFILE, Lab, _patient

BASE = "http://fhir.test/fhir"


@pytest.fixture
def registry(tmp_path) -> HospitalRegistry:
    folder = tmp_path / "profiles"
    folder.mkdir()
    (folder / "lab.yaml").write_text(PROFILE, encoding="utf-8")
    return HospitalRegistry.load_dir(folder)


@pytest.fixture(autouse=True)
def _no_hooks():
    hooks.reset()
    yield
    hooks.reset()


def _two_runs(registry, tmp_path) -> FakeFhir:
    server = FakeFhir()
    server._write("Patient", "p1", _patient())
    for run in ("run-a", "run-b"):
        lab = Lab(registry, server, tmp_path, run=run)
        for text in (lab.a04(), lab.orm(), lab.oru()):
            assert lab.send(text) == ("AA", "")
    server._write("Observation", "free", {"resourceType": "Observation", "status": "final"})
    return server


# -- fhir reset ---------------------------------------------------------------------------


def test_reset_deletes_one_run_and_nothing_else(registry, tmp_path):
    server = _two_runs(registry, tmp_path)
    run_b = {
        k: [r["id"] for r in server.all(k) if "run-b" in str(r.get("meta"))] for k in server.data
    }
    with HapiClient(BASE, transport=server.transport()) as client:
        plan = plan_reset(client, RunScope("run-a", LAB_TAG))
        assert {k: len(v) for k, v in plan.ids.items()} == {
            "Observation": 1,
            "DiagnosticReport": 1,
            "ServiceRequest": 1,
            "Encounter": 1,
        }
        assert execute_reset(client, plan) == 4
    for kind, ids in run_b.items():
        for server_id in ids:
            assert server_id in server.data[kind]
    assert "p1" in server.data["Patient"] and "free" in server.data["Observation"]
    assert not any("run-a" in str(r.get("meta")) for k in server.data for r in server.all(k))


def test_a_refused_delete_deletes_nothing(registry, tmp_path):
    server = _two_runs(registry, tmp_path)
    obs = next(r for r in server.all("Observation") if "run-a" in str(r.get("meta")))
    server._write(
        "DiagnosticReport",
        "outsider",
        {"resourceType": "DiagnosticReport", "result": [{"reference": f"Observation/{obs['id']}"}]},
    )
    before = {k: set(v) for k, v in server.data.items()}
    with HapiClient(BASE, transport=server.transport()) as client:
        plan = plan_reset(client, RunScope("run-a", LAB_TAG))
        with pytest.raises(Exception, match=r"returned 409"):
            execute_reset(client, plan)
    assert {k: set(v) for k, v in server.data.items()} == before


def _cli_server(monkeypatch, server: FakeFhir) -> None:
    def factory(base_url, **kwargs):
        kwargs.pop("transport", None)
        return HapiClient(base_url, transport=server.transport(), **kwargs)

    monkeypatch.setattr(cli, "HapiClient", factory)


def _reset_args(tmp_path, *extra: str) -> list[str]:
    return [
        "fhir",
        "reset",
        "--fhir-base",
        BASE,
        "--config",
        str(tmp_path / "profiles"),
        "--run-id",
        "run-a",
        *extra,
    ]


def test_reset_cli_is_a_dry_run_without_yes(registry, tmp_path, monkeypatch, capsys):
    server = _two_runs(registry, tmp_path)
    _cli_server(monkeypatch, server)
    before = {k: set(v) for k, v in server.data.items()}
    assert cli.main(_reset_args(tmp_path)) == 0
    assert "dry run" in capsys.readouterr().out
    assert {k: set(v) for k, v in server.data.items()} == before


@pytest.mark.parametrize("typed", ["run-b", "", "RUN-A"])
def test_reset_cli_needs_the_run_id_typed_back(registry, tmp_path, monkeypatch, typed):
    server = _two_runs(registry, tmp_path)
    _cli_server(monkeypatch, server)
    monkeypatch.setattr("builtins.input", lambda: typed)
    before = {k: set(v) for k, v in server.data.items()}
    assert cli.main(_reset_args(tmp_path, "--yes")) == 1
    assert {k: set(v) for k, v in server.data.items()} == before


def test_reset_cli_deletes_after_the_typed_run_id(registry, tmp_path, monkeypatch, capsys):
    server = _two_runs(registry, tmp_path)
    _cli_server(monkeypatch, server)
    monkeypatch.setattr("builtins.input", lambda: "run-a")
    assert cli.main(_reset_args(tmp_path, "--yes")) == 0
    assert "sent 4 deletes for run" in capsys.readouterr().out


def test_reset_cli_refuses_without_a_run_id(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HL7BRIDGE_RUN_ID", raising=False)
    args = ["fhir", "reset", "--fhir-base", BASE]
    assert cli.main(args) == 2
    assert "run id is required" in capsys.readouterr().err


# -- listen --target fhir guards --------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "env", "message"),
    [
        (["--target", "fhir", "--run-id", "a"], {}, "needs one named"),
        (["--target", "fhir", "--fhir-base", BASE], {}, "run id is required"),
        (["--target", "fhir", "--fhir-base", BASE, "--run-id", "a b"], {}, "value not shown"),
        (["--target", "fhir", "--fhir-base", BASE], {"HL7BRIDGE_RUN_ID": ""}, "value not shown"),
        (["--target", "fhir", "--fhir-base", BASE, "--run-id", "a"], {}, "needs match_only"),
        (["--run-id", "a"], {}, "--target fhir only"),
        (["--mapping-module", "x.py"], {}, "--target fhir only"),
    ],
)
def test_listen_direct_refuses_to_start(monkeypatch, capsys, tmp_path, args, env, message):
    monkeypatch.delenv("HL7BRIDGE_RUN_ID", raising=False)
    monkeypatch.delenv("HL7BRIDGE_FHIR_BASE", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["listen", *args, "--config", str(BUNDLED_PROFILES)]) == 2
    assert message in capsys.readouterr().err


# -- profile keys -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("  patient_policy: sometimes\n", "patient_policy"),
        ("  tag: {system: notauri, code: x}\n", "fhir.tag"),
        ("  tag: {system: urn:x, code: 'a b'}\n", "fhir.tag"),
        ("  allowed_processing_ids: [Prod]\n", "allowed_processing_ids"),
        ("  allowed_processing_ids: []\n", "allowed_processing_ids"),
        ("  identifier_systems:\n    mrn_by_authority: {A: 'x|y'}\n", "mrn_by_authority"),
        ("  resend_ignores: [MSH-7]\n", "unknown key 'resend_ignores'"),
    ],
)
def test_bad_fhir_blocks_are_refused(tmp_path, block, message):
    text = (BUNDLED_PROFILES / "hospa.yaml").read_text(encoding="utf-8") + "fhir:\n" + block
    path = tmp_path / "x.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_hospital_config(path)


def test_resend_ignores_is_not_a_profile_key(tmp_path):
    text = (BUNDLED_PROFILES / "hospa.yaml").read_text(encoding="utf-8") + "resend_ignores: []\n"
    path = tmp_path / "x.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="resend_ignores"):
        load_hospital_config(path)


def test_the_example_lab_profile_loads_as_match_only():
    registry = HospitalRegistry.load_dir(BUNDLED_PROFILES / "examples")
    lab = registry.by_id["WINTEHR"]
    assert lab.fhir.patient_policy == "match_only"
    assert lab.fhir.allowed_processing_ids == ("T", "D")
    assert "WINTEHR" not in HospitalRegistry.load_dir(BUNDLED_PROFILES).by_id


# -- from-fhir -----------------------------------------------------------------------------


def test_pick_mrn_prefers_mr_and_says_so():
    typed = pick_mrn(_patient())
    assert (typed.value, typed.typed_mr) == ("MRN001", True)
    untyped = pick_mrn({"identifier": [{"system": "urn:a", "value": "X1"}, {"value": "X2"}]})
    assert (untyped.value, untyped.typed_mr) == ("X1", False)
    with pytest.raises(Exception, match="no identifier"):
        pick_mrn({"identifier": [{"system": "urn:a"}]})


def test_escape_keeps_separators_out_of_fields():
    assert escape("A|B^C&D~E\\F\rG") == "A\\F\\B\\S\\C\\T\\D\\R\\E\\E\\F G"


def test_rendered_messages_map_back_with_the_patient_values(registry):
    patient = _patient()
    patient["name"] = [{"family": "ZZZ|PIPE", "given": ["A^B"]}]
    r = Renderer(registry.by_id["LAB"], NOW)
    mrn = pick_mrn(patient)
    for text in (
        r.adt("A04", patient, mrn, "V1"),
        r.orm(patient, mrn, "ACC1"),
        r.oru(patient, mrn, "ACC1"),
    ):
        assert "|T|2.5.1" in text.split("\r")[0]
        mapped = message_to_bundle(text, registry)
        first = mapped.bundle.entry[0].resource
        assert first.identifier[0].value == "MRN001"
        assert first.name[0].family == "ZZZ|PIPE"


def test_report_message_renders_results(registry, tmp_path):
    server = FakeFhir()
    server._write("Patient", "p1", _patient())
    lab = Lab(registry, server, tmp_path)
    assert lab.send(lab.oru()) == ("AA", "")
    report = server.all("DiagnosticReport")[0]
    with HapiClient(BASE, transport=server.transport()) as client:
        text, mrn, skipped = report_message(client, report["id"], registry.by_id["LAB"], NOW)
    assert skipped == 0 and mrn.value == "MRN001"
    mapped = message_to_bundle(text, registry)
    kinds = mapped.entry_types()
    assert kinds.count("Observation") == 1 and kinds[-1] == "DiagnosticReport"


def test_from_fhir_cli_writes_four_files(registry, tmp_path, monkeypatch, capsys):
    server = FakeFhir()
    server._write("Patient", "p1", _patient())
    _cli_server(monkeypatch, server)
    out = tmp_path / "msgs"
    args = ["from-fhir", "--patient", "p1", "--hospital", "LAB", "--out", str(out)]
    args += ["--fhir-base", BASE, "--config", str(tmp_path / "profiles")]
    assert cli.main(args) == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "01_adt_a04.hl7",
        "02_adt_a08.hl7",
        "03_orm_o01.hl7",
        "04_oru_r01.hl7",
    ]
    assert "typed MR" in capsys.readouterr().out
    assert cli.main(args) == 2  # never overwrites


# -- mapping hooks -------------------------------------------------------------------------


def _hook_file(tmp_path, body: str):
    path = tmp_path / "lab_mapping.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_hooks_replace_the_three_steps(tmp_path, registry):
    path = _hook_file(
        tmp_path,
        "from datetime import datetime, timezone\n"
        "def _helper():\n    return 1\n"
        "def sex_code(raw):\n    return 'male' if raw == 'F' else 'female'\n"
        "def abnormal_flag(raw):\n    return 'H'\n"
        "def observation_time(raw, zone):\n"
        "    return datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)\n",
    )
    assert hooks.load_mapping_module(path) == ("abnormal_flag", "observation_time", "sex_code")
    r = Renderer(registry.by_id["LAB"], NOW)
    text = r.oru(_patient(), pick_mrn(_patient()), "ACC1")
    mapped = message_to_bundle(text, registry)
    resources = [e.resource for e in mapped.bundle.entry]
    assert resources[0].gender == "male"
    obs = resources[1]
    assert obs.interpretation[0].coding[0].code == "H"
    assert obs.effectiveDateTime.isoformat().startswith("2026-01-02T03:04")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("def sex_code(raw):\n    return 'M'\n", "PID-8"),
        ("def sex_code(raw):\n    raise ValueError(raw)\n", "PID-8"),
        ("def abnormal_flag(raw):\n    return 'HIGH'\n", "OBX-8"),
        (
            "from datetime import datetime\n"
            "def observation_time(raw, zone):\n    return datetime(2026, 1, 1)\n",
            "OBX-14",
        ),
    ],
)
def test_a_hook_that_returns_garbage_is_a_mapping_error(tmp_path, registry, body, field):
    hooks.load_mapping_module(_hook_file(tmp_path, body))
    r = Renderer(registry.by_id["LAB"], NOW)
    with pytest.raises(MappingError) as caught:
        message_to_bundle(r.oru(_patient(), pick_mrn(_patient()), "ACC1"), registry)
    assert caught.value.field == field
    assert "MRN001" not in str(caught.value) and "ZZZTEST" not in str(caught.value)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("def sex_codes(raw):\n    return 'male'\n", "not a mapping hook"),
        ("raise RuntimeError('boom MRN001')\n", "failed to import"),
    ],
)
def test_a_bad_hook_file_is_refused(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message) as caught:
        hooks.load_mapping_module(_hook_file(tmp_path, body))
    assert "MRN001" not in str(caught.value)


def test_a_hook_file_must_be_a_py_file(tmp_path):
    with pytest.raises(ConfigError):
        hooks.load_mapping_module(tmp_path / "missing.py")


def test_to_fhir_loads_hooks_and_warns(tmp_path, registry, capsys):
    path = _hook_file(tmp_path, "def sex_code(raw):\n    return 'other'\n")
    r = Renderer(registry.by_id["LAB"], datetime(2026, 11, 20, tzinfo=UTC))
    msg = tmp_path / "a.hl7"
    msg.write_bytes(r.adt("A04", _patient(), pick_mrn(_patient()), "V1").encode())
    args = [
        "to-fhir",
        str(msg),
        "--config",
        str(tmp_path / "profiles"),
        "--mapping-module",
        str(path),
    ]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert '"gender": "other"' in captured.out
    assert "teaching lab only" in captured.err


def test_reset_refuses_a_search_result_without_the_run_tag(registry, tmp_path):
    from hl7bridge.fhir.reset import ResetRefused

    server = _two_runs(registry, tmp_path)

    def untag(request):
        answer = server(request)
        if request.method == "GET" and "_tag" in request.url.params:
            body = answer.json()
            for entry in body.get("entry", []):
                entry["resource"]["meta"]["tag"] = []
            return httpx.Response(200, json=body)
        return answer

    before = {k: set(v) for k, v in server.data.items()}
    with (
        HapiClient(BASE, transport=httpx.MockTransport(untag)) as client,
        pytest.raises(ResetRefused),
    ):
        plan_reset(client, RunScope("run-a", LAB_TAG))
    assert {k: set(v) for k, v in server.data.items()} == before
