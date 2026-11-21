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
from hl7bridge.errors import EXIT_CONNECTION, EXIT_FINDINGS, EXIT_OK, EXIT_USAGE, ConnectError
from hl7bridge.explain import explain_message, render_text, split_messages
from hl7bridge.fhir.client import HapiClient
from hl7bridge.ingest import IngestPipeline
from hl7bridge.ingest import mllp as mllp_module
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.store import connect

from ..fakefhir import FakeFhir
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


def test_send_expect_ae_prints_an_unexpected_aa_and_not_the_expected_ae(tmp_path, capsys):
    broken = SAMPLES_DIR / "11_broken_missing_accession.hl7"
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "a.hl7").write_bytes(A04.read_bytes())
    (folder / "b.hl7").write_bytes(broken.read_bytes())
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(folder), "--port", str(listener.port), "--expect", "AE"])
    out = capsys.readouterr().out
    assert rc == EXIT_FINDINGS
    assert "a.hl7: AA (" in out
    assert "b.hl7: AE" not in out


def test_send_splits_a_batch_into_one_frame_per_message(tmp_path, capsys):
    """Two messages in one frame would get one ACK and be read as one."""
    db = tmp_path / "s.db"
    batch = tmp_path / "batch.hl7"
    batch.write_bytes(A04.read_bytes() + ORU.read_bytes())
    with _Listener(db) as listener:
        rc = cli.main(["send", str(batch), "--port", str(listener.port)])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "sent 2 message(s)" in out and "got 2 ACK(s)" in out
    conn = connect(db)
    types = [r[0] for r in conn.execute("SELECT message_type FROM messages ORDER BY id")]
    conn.close()
    assert types == ["ADT^A04", "ORU^R01"]


def test_send_refuses_input_that_does_not_start_with_msh(tmp_path, capsys):
    path = tmp_path / "x.hl7"
    path.write_bytes(b"PID|1\r" + A04.read_bytes())
    assert cli.main(["send", str(path)]) == EXIT_USAGE
    assert "x.hl7 does not start with an MSH segment; nothing sent." in capsys.readouterr().err


def test_send_warns_when_the_host_is_not_loopback(monkeypatch, capsys):
    async def refuse(*_a, **_k):
        raise ConnectError(ConnectionRefusedError())

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


def test_replay_from_the_store_sends_the_exact_stored_bytes(tmp_path, capsys):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    target = tmp_path / "target.db"
    with _Listener(target) as listener:
        rc = cli.main(["replay", "--db", str(db), "--from", "store", "--port", str(listener.port)])
    assert rc == EXIT_OK
    capsys.readouterr()

    def raws(path: Path) -> list[str]:
        conn = connect(path)
        rows = [r["raw"] for r in conn.execute("SELECT raw FROM messages ORDER BY raw")]
        conn.close()
        return rows

    assert raws(target) == raws(db)


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
        (["--since", "2026-10-07T00:00:00"], "--since '2026-10-07T00:00:00' has no UTC offset"),
        (["--since", "yesterday"], "--since 'yesterday' is not an ISO 8601 timestamp"),
    ],
)
def test_replay_usage_errors(tmp_path, capsys, argv, message):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    assert cli.main(["replay", "--db", str(db), *argv]) == EXIT_USAGE
    assert message in capsys.readouterr().err


def test_replay_sends_each_distinct_message_once(tmp_path, capsys):
    """A sender that retried put the same bytes in the quarantine twice;
    replaying into the listener that filled it must not multiply it."""
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    conn = connect(db)
    pipeline = IngestPipeline(HospitalRegistry.load_dir(BUNDLED_PROFILES), conn)
    pipeline.handle_bytes((SAMPLES_DIR / "11_broken_missing_accession.hl7").read_bytes())
    assert (
        conn.execute("SELECT COUNT(*) FROM quarantine WHERE reason != 'mllp_junk'").fetchone()[0]
        == 4
    )
    conn.close()
    assert cli.main(["replay", "--db", str(db), "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "skipping 1 duplicate(s): the same bytes as an older row" in out
    assert "3 message(s) would be sent" in out


def test_replay_reads_a_version_2_store(tmp_path, capsys):
    """Version 2 has the same messages and quarantine tables, so replay is
    the way to carry an old store's messages into a new listener."""
    db = tmp_path / "old.db"
    _store_with_rejects(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE schema_version SET version = 2")
    conn.commit()
    conn.close()
    assert cli.main(["replay", "--db", str(db), "--dry-run"]) == EXIT_OK
    assert "3 message(s) would be sent" in capsys.readouterr().out
    conn = sqlite3.connect(db)
    conn.execute("UPDATE schema_version SET version = 1")
    conn.commit()
    conn.close()
    assert cli.main(["replay", "--db", str(db), "--dry-run"]) == EXIT_USAGE
    assert "has schema version 1" in capsys.readouterr().err


def test_replay_dry_run_strips_control_characters(tmp_path, capsys):
    db = tmp_path / "src.db"
    _store_with_rejects(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE messages SET message_type = 'ADT^A04\x1b[2J'")
    conn.commit()
    conn.close()
    assert cli.main(["replay", "--db", str(db), "--dry-run", "--from", "store"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "\x1b" not in out and "ADT^A04?[2J" in out


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
    # The TX line is report text (conclusion), not an Observation.
    assert [e["resource"]["resourceType"] for e in bundle["entry"]] == [
        "Patient",
        "Observation",
        "Observation",
        "DiagnosticReport",
    ]


def test_to_fhir_writes_decimals_as_sent(tmp_path, capsys):
    path = tmp_path / "n.hl7"
    path.write_bytes(ORU.read_bytes().replace(b"|12.5|", b"|12.50|"))
    assert cli.main(["to-fhir", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert '"value": 12.50,' in out
    out_dir = tmp_path / "o"
    assert cli.main(["to-fhir", str(path), "--out", str(out_dir)]) == EXIT_OK
    assert b'"value": 12.50,' in (out_dir / "n.json").read_bytes()
    capsys.readouterr()


def test_to_fhir_refuses_bytes_that_are_not_utf8(tmp_path, capsys):
    """Lossy decoding would turn two different bad bytes into the same
    U+FFFD and two patients into one key."""
    path = tmp_path / "bad.hl7"
    path.write_bytes(A04.read_bytes().replace(b"SA1001", b"SA1\xff01"))
    rc = cli.main(["to-fhir", str(path)])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS
    assert "error: bad.hl7: not valid UTF-8; not converted." in captured.err
    assert captured.out == ""


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
    assert "wrote 12 Bundle(s)" in err
    assert "3 of 15 message(s) could not be mapped" in err
    names = sorted(p.name for p in out.glob("*.json"))
    assert names[0] == "01_hospa_adt_a04.json" and len(names) == 12
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


class _TransactionServer(FakeFhir):
    """The in-memory server, failing the nth transaction when asked."""

    def __init__(self, fail_on: int | None = None) -> None:
        super().__init__()
        self.fail_on = fail_on

        def maybe_fail(body: dict) -> httpx.Response | None:
            if self.fail_on is not None and len(self.transactions) == self.fail_on:
                entries = [{"response": {"status": "400 Bad Request"}} for _ in body["entry"]]
                return httpx.Response(
                    200,
                    json={
                        "resourceType": "Bundle",
                        "type": "transaction-response",
                        "entry": entries,
                    },
                )
            return None

        self.before_commit = maybe_fail

    @property
    def posted(self) -> list[dict]:
        return self.transactions


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
        "15_hospc_adt_a08_discharge.hl7: committed (Patient 200, Encounter 200)\n"
    )
    assert [b["entry"][1]["resource"]["status"] for b in server.posted] == [
        "in-progress",
        "finished",
    ]
    # The A08 went to the resources the A01 made, by id, with If-Match.
    assert server.posted[1]["entry"][0]["request"]["ifMatch"] == 'W/"1"'
    assert server.posted[1]["entry"][1]["request"]["url"].startswith("Encounter/")


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


def _two_with_one_broken(tmp_path):
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "1.hl7").write_bytes((SAMPLES_DIR / "02_hospa_orm_o01.hl7").read_bytes())
    (folder / "2.hl7").write_bytes(
        (SAMPLES_DIR / "02_hospa_orm_o01.hl7").read_bytes().replace(b"ORC|NW|", b"ORC|XO|")
    )
    (folder / "3.hl7").write_bytes((SAMPLES_DIR / "03_hospa_oru_r01_final.hl7").read_bytes())
    return folder


def test_to_fhir_post_sends_nothing_when_one_message_fails_to_map(tmp_path, monkeypatch, capsys):
    """A later message can depend on a failed one, so a run with a failure
    posts nothing unless --skip-failures says to go on."""
    server = _TransactionServer()
    _patch(monkeypatch, server)
    folder = _two_with_one_broken(tmp_path)
    base = ["--post", "--fhir-base", "http://fhir.test/fhir"]
    rc = cli.main(["to-fhir", str(folder), *base])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS
    assert server.posted == []
    assert "error: 2.hl7: ORC-1: code not in table 0119" in captured.err
    assert "nothing posted: 1 message(s) could not be mapped" in captured.err
    rc = cli.main(["to-fhir", str(folder), *base, "--skip-failures"])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS
    assert len(server.posted) == 2
    assert "1 of 3 message(s) could not be mapped" in captured.err


def test_skip_failures_needs_post(capsys):
    assert cli.main(["to-fhir", str(A04), "--skip-failures"]) == EXIT_USAGE
    assert "--skip-failures applies to --post only." in capsys.readouterr().err


def test_to_fhir_post_refuses_an_older_report_after_a_newer_one(tmp_path, monkeypatch, capsys):
    server = _TransactionServer()
    _patch(monkeypatch, server)
    folder = tmp_path / "m"
    folder.mkdir()
    final = (SAMPLES_DIR / "03_hospa_oru_r01_final.hl7").read_bytes()
    (folder / "a_final.hl7").write_bytes(final)
    (folder / "b_prelim.hl7").write_bytes(
        final.replace(b"|CR|F", b"|CR|P").replace(b"SMPA0003", b"SMPA0103")
    )
    rc = cli.main(["to-fhir", str(folder), "--post", "--fhir-base", "http://fhir.test/fhir"])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS
    assert server.posted == []
    assert "b_prelim.hl7 holds an older version of the same report as a_final.hl7" in (captured.err)
    # A later OBR-22 wins over the status order: a correction issued later
    # than a final is fine, and so is the final after its preliminary.
    (folder / "b_prelim.hl7").unlink()
    (folder / "0_prelim.hl7").write_bytes(
        final.replace(b"|CR|F", b"|CR|P").replace(b"SMPA0003", b"SMPA0103")
    )
    rc = cli.main(["to-fhir", str(folder), "--post", "--fhir-base", "http://fhir.test/fhir"])
    assert rc == EXIT_OK, capsys.readouterr()


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
