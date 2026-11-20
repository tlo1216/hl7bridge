"""The 0.4 message commands: explain, send from stdin with --repeat,
--rate and --expect, replay, and to-fhir (offline and --post)."""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest

from hl7bridge import cli
from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import EXIT_CONNECTION, EXIT_FINDINGS, EXIT_OK, EXIT_USAGE
from hl7bridge.explain import explain_message, render_text, split_messages
from hl7bridge.fhir.client import HapiClient
from hl7bridge.ingest import IngestPipeline
from hl7bridge.ingest import mllp as mllp_module
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.store import connect

from .test_cli import _Listener, clean_env, restore_logging  # noqa: F401  autouse fixtures

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "explain"
ORU = SAMPLES_DIR / "14_hospa_oru_r01_structured.hl7"
A04 = SAMPLES_DIR / "01_hospa_adt_a04.hl7"


def _stdin(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(data)))


# -- explain --------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(SAMPLES_DIR.glob("*.hl7")), ids=lambda p: p.name)
def test_explain_matches_its_golden_file(path):
    import os

    got = render_text(explain_message(path.read_bytes().decode())) + "\n"
    golden = GOLDEN / (path.stem + ".txt")
    if os.environ.get("HL7BRIDGE_UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_bytes(got.encode("utf-8"))
    assert golden.read_text(encoding="utf-8") == got


def test_explain_lines_name_fields_split_components_and_decode_codes():
    text = render_text(explain_message(ORU.read_bytes().decode()))
    assert "  PID-5  Patient name: ZZZSAMPLE^ONE\n" in text
    assert "      PID-5.1 family name: ZZZSAMPLE\n" in text
    assert "  OBR-25 Result status: F = final\n" in text
    assert "  MSH-11 Processing id: T = training\n" in text
    assert "      OBX-5.2 text: Synthetic finding present\n" in text  # OBX-2 CWE
    assert "OBX 2  observation result" in text
    assert "PID-2" not in text, "empty fields are hidden"


def test_explain_all_shows_empty_fields_and_repetitions():
    raw = "MSH|^~\\&|A|B|C|D|20261007||ADT^A04|1|T|2.3\rPID|1||M1~M2^^^H^MR||DOE^J\r"
    text = render_text(explain_message(raw, show_all=True))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    assert "PID-2 Patient id (old): (empty)" in lines
    assert "PID-3 Patient identifier list: M1" in lines
    assert "PID-3[2] Patient identifier list: M2^^^H^MR" in lines
    assert "PID-3.4 assigning authority: H" in lines


def test_explain_unknown_codes_and_z_segments():
    raw = "MSH|^~\\&|A|B|C|D|20261007||ADT^A04|1|Q|2.3\rZBI|1|X\rQQQ|7\r"
    text = render_text(explain_message(raw))
    assert "MSH-11 Processing id: Q = (not a known code)" in text
    assert "ZBI  site-specific segment\n  ZBI-1: 1" in text
    assert "QQQ\n  QQQ-1: 7" in text


def test_explain_decodes_escapes_and_cleans_control_characters():
    raw = "MSH|^~\\&|A|B|C|D|20261007||ADT^A04|1|T|2.3\rNTE|1||a\\T\\b\\F\\c\x1b[31m\r"
    text = render_text(explain_message(raw))
    assert "NTE-3  Comment: a&b|c?[31m" in text


@pytest.mark.parametrize("raw", ["", "PID|1\r", "MSH|\r", "MSH|^~|x\r", "MSH|^~\\&|A\rP!D|1\r"])
def test_explain_refuses_what_is_not_hl7(raw):
    from hl7bridge.errors import HL7ParseError

    with pytest.raises(HL7ParseError):
        explain_message(raw)


def test_split_messages_handles_batches_and_line_ends():
    two = A04.read_bytes().decode() + ORU.read_bytes().decode().replace("\r", "\n")
    assert [m.split("|", 9)[8] for m in split_messages(two)] == ["ADT^A04", "ORU^R01"]


def test_cli_explain_file_stdin_and_json(monkeypatch, capsys):
    assert cli.main(["explain", str(ORU)]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("== 14_hospa_oru_r01_structured.hl7\nMSH  message header\n")
    _stdin(monkeypatch, A04.read_bytes())
    assert cli.main(["explain", "-", "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data[0]["file"] == "<stdin>"
    pid = next(s for s in data[0]["segments"] if s["segment"] == "PID")
    sex = next(f for f in pid["fields"] if f["position"] == "PID-8")
    assert (sex["value"], sex["meaning"]) == ("F", "female")


def test_cli_explain_a_broken_file_exits_1_without_quoting_it(tmp_path, capsys):
    bad = tmp_path / "bad.hl7"
    bad.write_bytes(b"PID|CANARY0042\r")
    good = tmp_path / "good.hl7"
    good.write_bytes(A04.read_bytes())
    assert cli.main(["explain", str(tmp_path)]) == EXIT_FINDINGS
    captured = capsys.readouterr()
    assert "error: bad.hl7: not parseable as HL7 v2.\n" in captured.err
    assert "CANARY0042" not in captured.err
    assert "== good.hl7" in captured.out


def test_cli_explain_logs_nothing(caplog, capsys):
    with caplog.at_level(logging.DEBUG):
        cli.main(["explain", str(ORU), "-vv"])
    assert "ZZZSAMPLE" not in caplog.text
    capsys.readouterr()


def test_cli_explain_notes_bytes_that_are_not_utf8(tmp_path, capsys):
    path = tmp_path / "latin.hl7"
    path.write_bytes(A04.read_bytes().replace(b"ONE", b"\xd6NE"))
    assert cli.main(["explain", str(path)]) == EXIT_OK
    captured = capsys.readouterr()
    assert "note: latin.hl7 is not valid UTF-8" in captured.err
    assert "ZZZSAMPLE^�NE" in captured.out


# -- send ----------------------------------------------------------------------------


def test_send_from_stdin(tmp_path, monkeypatch, capsys):
    _stdin(monkeypatch, A04.read_bytes())
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", "-", "--port", str(listener.port)])
    assert rc == EXIT_OK
    assert "sent 1 message(s) from <stdin>, got 1 ACK(s)\n  AA: 1\n" in capsys.readouterr().out


def test_send_repeat_sends_the_same_bytes_and_stores_one(tmp_path, capsys):
    db = tmp_path / "s.db"
    with _Listener(db) as listener:
        rc = cli.main(["send", str(A04), "--port", str(listener.port), "--repeat", "3"])
    assert rc == EXIT_OK
    assert "sent 3 message(s)" in capsys.readouterr().out
    conn = connect(db)
    row = conn.execute("SELECT COUNT(*), MAX(duplicate_count) FROM messages").fetchone()
    conn.close()
    assert tuple(row) == (1, 2)


def test_send_rate_pauses_between_messages(tmp_path, monkeypatch, capsys):
    pauses: list[float] = []
    real_sleep = mllp_module.asyncio.sleep

    async def fake_sleep(seconds, *a, **k):
        pauses.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(mllp_module.asyncio, "sleep", fake_sleep)
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(
            ["send", str(A04), "--port", str(listener.port), "--repeat", "3", "--rate", "4"]
        )
    assert rc == EXIT_OK
    assert pauses == [0.25, 0.25]  # between messages, not before the first
    capsys.readouterr()


@pytest.mark.parametrize("value", ["0", "-1", "1001", "fast"])
def test_send_rate_must_be_sensible(value, capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["send", str(A04), "--rate", value])
    assert info.value.code == EXIT_USAGE
    capsys.readouterr()


def test_send_expect_ae_passes_on_ae_and_fails_on_aa(tmp_path, capsys):
    broken = SAMPLES_DIR / "11_broken_missing_accession.hl7"
    with _Listener(tmp_path / "s.db") as listener:
        rc_ae = cli.main(["send", str(broken), "--port", str(listener.port), "--expect", "AE"])
        out_ae = capsys.readouterr().out
        rc_aa = cli.main(["send", str(A04), "--port", str(listener.port), "--expect", "AE"])
        out_aa = capsys.readouterr().out
    assert rc_ae == EXIT_OK
    assert "  expected AE: 1 of 1\n" in out_ae
    assert rc_aa == EXIT_FINDINGS
    assert "  expected AE: 0 of 1\n" in out_aa


def test_send_warns_when_the_host_is_not_loopback(monkeypatch, capsys):
    async def refuse(*_a, **_k):
        raise ConnectionRefusedError

    monkeypatch.setattr(cli, "send_messages", refuse)
    rc = cli.main(["send", str(A04), "--host", "192.0.2.10"])
    assert rc == EXIT_CONNECTION
    assert "warning: sending to 192.0.2.10, which is not loopback" in capsys.readouterr().err


def test_send_on_loopback_does_not_warn(tmp_path, capsys):
    with _Listener(tmp_path / "s.db") as listener:
        cli.main(["send", str(A04), "--port", str(listener.port)])
    assert "warning" not in capsys.readouterr().err


# -- replay --------------------------------------------------------------------------


def _store_with_rejects(db: Path) -> None:
    """Samples, plus an A01 from HOSPA (which its profile does not list) and
    junk bytes, so the quarantine holds three kinds of row."""
    conn = connect(db)
    pipeline = IngestPipeline(HospitalRegistry.load_dir(BUNDLED_PROFILES), conn)
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        pipeline.handle_bytes(path.read_bytes())
    a01 = A04.read_bytes().replace(b"ADT^A04|SMPA0001", b"ADT^A01|SMPA0099")
    pipeline.handle_bytes(a01)
    import asyncio

    asyncio.run(pipeline.handle_junk(b"junk outside framing"))
    conn.close()


def _snapshot(db: Path) -> bytes:
    return db.read_bytes()


def test_replay_dry_run_lists_rows_oldest_first_and_sends_nothing(tmp_path, capsys):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    before = _snapshot(db)
    assert cli.main(["replay", "--db", str(db), "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[-1] == "3 message(s) would be sent, oldest first (dry run: nothing sent)"
    assert "missing_accession" in out and "bad_timestamp" in out
    assert "unexpected_message_type" in out
    assert "mllp_junk" not in out, "junk is not a message"
    for canary in ("SMPA", "ZZZSAMPLE", "SA1001"):
        assert canary not in out
    assert _snapshot(db) == before, "replay opens the store read-only"


def test_replay_after_a_profile_fix_is_accepted(tmp_path, capsys):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    fixed = tmp_path / "profiles"
    fixed.mkdir()
    for source in BUNDLED_PROFILES.glob("*.yaml"):
        text = source.read_text(encoding="utf-8")
        if source.name == "hospa.yaml":
            text = text.replace("[ADT^A04,", "[ADT^A01, ADT^A04,")
        (fixed / source.name).write_text(text, encoding="utf-8")
    target = tmp_path / "target.db"
    listener = _Listener(target)
    listener._registry = HospitalRegistry.load_dir(fixed)
    with listener:
        rc = cli.main(
            [
                "replay",
                "--db",
                str(db),
                "--only-reason",
                "unexpected_message_type",
                "--port",
                str(listener.port),
            ]
        )
    out = capsys.readouterr().out
    assert rc == EXIT_OK, out
    assert "sent 1 message(s) from the quarantine, got 1 ACK(s)\n  AA: 1\n" in out
    conn = connect(target)
    stored = conn.execute("SELECT raw FROM messages").fetchone()["raw"]
    conn.close()
    original = A04.read_bytes().replace(b"ADT^A04|SMPA0001", b"ADT^A01|SMPA0099")
    assert stored.encode() == original, "the exact bytes, nothing restamped"


def test_replay_from_the_store_is_all_duplicates_on_the_same_store(tmp_path, capsys):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    with _Listener(db) as listener:
        rc = cli.main(["replay", "--db", str(db), "--from", "store", "--port", str(listener.port)])
    out = capsys.readouterr().out
    assert rc == EXIT_OK, out
    assert "sent 13 message(s) from the store, got 13 ACK(s)\n  AA: 13\n" in out
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 13
    assert conn.execute("SELECT MIN(duplicate_count) FROM messages").fetchone()[0] == 1
    conn.close()


def test_replay_failures_name_the_row_not_the_message(tmp_path, capsys):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    with _Listener(tmp_path / "t.db") as listener:
        rc = cli.main(
            [
                "replay",
                "--db",
                str(db),
                "--only-reason",
                "bad_timestamp",
                "--port",
                str(listener.port),
            ]
        )
    out = capsys.readouterr().out
    assert rc == EXIT_FINDINGS
    assert "quarantine #2: AE (application error" in out
    assert "  next: hl7bridge replay --dry-run --from quarantine\n" in out
    assert "SMPA" not in out


def test_replay_filters(tmp_path, capsys):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    cli.main(["replay", "--db", str(db), "--dry-run", "--hospital", "HOSPB"])
    out = capsys.readouterr().out
    assert "1 message(s) would be sent" in out and "HOSPB" in out
    cli.main(["replay", "--db", str(db), "--dry-run", "--since", "2999-01-01T00:00:00+00:00"])
    assert capsys.readouterr().out == "nothing to replay from the quarantine\n"
    cli.main(["replay", "--db", str(db), "--dry-run", "--from", "store", "--limit", "2"])
    assert "2 message(s) would be sent" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--only-reason", "mllp_junk"], "--only-reason takes one of"),
        (["--only-reason", "nope"], "--only-reason takes one of"),
        (["--from", "store", "--only-reason", "bad_timestamp"], "applies to --from quarantine"),
        (["--since", "2026-10-07T00:00:00"], "has no UTC offset"),
    ],
)
def test_replay_usage_errors(tmp_path, capsys, argv, message):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    assert cli.main(["replay", "--db", str(db), *argv]) == EXIT_USAGE
    assert message in capsys.readouterr().err


def test_replay_never_creates_a_database(tmp_path, capsys):
    db = tmp_path / "none.db"
    assert cli.main(["replay", "--db", str(db), "--dry-run"]) == EXIT_USAGE
    assert not db.exists()
    assert "no database at" in capsys.readouterr().err


def test_replay_store_is_never_written(tmp_path, capsys, monkeypatch):
    """Even a write attempted through the replay connection fails."""
    db = tmp_path / "src.db"
    _store_with_rejects(db)

    class Args:
        db = str(tmp_path / "src.db")
        quiet = True

    conn = cli._readonly_db(Args())  # type: ignore[arg-type]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM quarantine")
    conn.close()
    capsys.readouterr()


# -- to-fhir -------------------------------------------------------------------------


def test_to_fhir_prints_one_bundle(capsys):
    assert cli.main(["to-fhir", str(ORU)]) == EXIT_OK
    bundle = json.loads(capsys.readouterr().out)
    assert bundle["type"] == "transaction"
    assert [e["resource"]["resourceType"] for e in bundle["entry"]] == [
        "Patient",
        "Observation",
        "Observation",
        "Observation",
        "DiagnosticReport",
    ]


def test_to_fhir_reads_stdin(monkeypatch, capsys):
    _stdin(monkeypatch, A04.read_bytes())
    assert cli.main(["to-fhir", "-"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["entry"][0]["resource"]["resourceType"] == (
        "Patient"
    )


def test_to_fhir_folder_needs_out(capsys):
    assert cli.main(["to-fhir", str(SAMPLES_DIR)]) == EXIT_USAGE
    assert "15 messages need a folder for their Bundles. Pass --out DIR" in capsys.readouterr().err


def test_to_fhir_out_writes_each_bundle_and_never_overwrites(tmp_path, capsys):
    out = tmp_path / "bundles"
    assert cli.main(["to-fhir", str(SAMPLES_DIR), "--out", str(out)]) == EXIT_FINDINGS
    err = capsys.readouterr().err
    assert "wrote 11 Bundle(s)" in err
    assert "4 of 15 message(s) could not be mapped" in err
    assert "error: 08_hospc_adt_a40_merge.hl7: MSH-9: message type is not mapped" in err
    names = sorted(p.name for p in out.glob("*.json"))
    assert names[0] == "01_hospa_adt_a04.json" and len(names) == 11
    first = (out / names[0]).read_bytes()
    assert cli.main(["to-fhir", str(A04), "--out", str(out)]) == EXIT_USAGE
    assert "already exists; to-fhir never overwrites a file" in capsys.readouterr().err
    assert (out / names[0]).read_bytes() == first


def test_to_fhir_names_messages_in_a_batch_file(tmp_path, capsys):
    batch = tmp_path / "batch.hl7"
    batch.write_bytes(A04.read_bytes() + ORU.read_bytes())
    out = tmp_path / "b"
    assert cli.main(["to-fhir", str(batch), "--out", str(out)]) == EXIT_OK
    assert sorted(p.name for p in out.iterdir()) == ["batch-1.json", "batch-2.json"]
    capsys.readouterr()


def test_to_fhir_errors_never_quote_the_message(tmp_path, capsys, caplog):
    canary = "CANARY0077"
    path = tmp_path / "a.hl7"
    path.write_bytes(A04.read_bytes().replace(b"|F\r", f"|{canary}\r".encode()))
    with caplog.at_level(logging.DEBUG):
        rc = cli.main(["to-fhir", str(path), "-vv"])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS
    assert "error: a.hl7: PID-8: code not in table 0001" in captured.err
    assert canary not in captured.err + captured.out + caplog.text


class _TransactionServer:
    def __init__(self, fail_on: int | None = None) -> None:
        self.posted: list[dict] = []
        self.fail_on = fail_on

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"resourceType": "CapabilityStatement"})
        bundle = json.loads(request.content)
        self.posted.append(bundle)
        status = "201 Created"
        if self.fail_on is not None and len(self.posted) == self.fail_on:
            status = "400 Bad Request"
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [{"response": {"status": status}} for _ in bundle["entry"]],
            },
        )


def _patch(monkeypatch, server) -> None:
    def factory(base_url: str, **kwargs):
        kwargs.pop("transport", None)
        return HapiClient(base_url, transport=httpx.MockTransport(server), **kwargs)

    monkeypatch.setattr(cli, "HapiClient", factory)


def test_to_fhir_post_commits_each_bundle_in_order(tmp_path, monkeypatch, capsys):
    server = _TransactionServer()
    _patch(monkeypatch, server)
    folder = tmp_path / "m"
    folder.mkdir()
    for name in ("13_hospc_adt_a01_inpatient.hl7", "15_hospc_adt_a08_discharge.hl7"):
        (folder / name).write_bytes((SAMPLES_DIR / name).read_bytes())
    rc = cli.main(["to-fhir", str(folder), "--post", "--fhir-base", "http://fhir.test/fhir"])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert out == (
        "13_hospc_adt_a01_inpatient.hl7: committed (Patient 201, Encounter 201)\n"
        "15_hospc_adt_a08_discharge.hl7: committed (Patient 201, Encounter 201)\n"
    )
    assert [b["entry"][1]["resource"]["status"] for b in server.posted] == [
        "in-progress",
        "finished",
    ]


def test_to_fhir_post_stops_at_the_first_bundle_not_committed(tmp_path, monkeypatch, capsys):
    server = _TransactionServer(fail_on=1)
    _patch(monkeypatch, server)
    folder = tmp_path / "m"
    folder.mkdir()
    for name in ("13_hospc_adt_a01_inpatient.hl7", "15_hospc_adt_a08_discharge.hl7"):
        (folder / name).write_bytes((SAMPLES_DIR / name).read_bytes())
    rc = cli.main(["to-fhir", str(folder), "--post", "--fhir-base", "http://fhir.test/fhir"])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS
    assert len(server.posted) == 1
    assert "entry 1 was not committed" in captured.err
    assert "committed" not in captured.out


def test_to_fhir_post_needs_a_named_server(capsys):
    assert cli.main(["to-fhir", str(A04), "--post"]) == EXIT_USAGE
    assert "to-fhir --post writes to a FHIR server, so it needs one named" in (
        capsys.readouterr().err
    )


def test_to_fhir_post_sends_nothing_when_no_message_maps(tmp_path, monkeypatch, capsys):
    server = _TransactionServer()
    _patch(monkeypatch, server)
    rc = cli.main(
        [
            "to-fhir",
            str(SAMPLES_DIR / "10_hospc_ack_r01.hl7"),
            "--post",
            "--fhir-base",
            "http://fhir.test/fhir",
        ]
    )
    assert rc == EXIT_FINDINGS
    assert server.posted == []
    capsys.readouterr()
