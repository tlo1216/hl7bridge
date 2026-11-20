"""CLI behavior: command layout, defaults, exit codes, error message text,
the listener's bind handling, send, --json, init, doctor and profiles.

Every user-facing message from the error table in the docs has a test on
its exact text here (or in test_config.py for profile keys)."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import socket
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from hl7bridge import __version__, cli
from hl7bridge.errors import EXIT_CONNECTION, EXIT_FINDINGS, EXIT_OK, EXIT_USAGE, StoreError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.ingest.mllp import encode_mllp
from hl7bridge.logsafety import QUIET_LOGGERS, THIRD_PARTY_HTTP_LOGGERS
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR, resolve_db, resolve_profiles
from hl7bridge.store import connect

CONFIG_DIR = BUNDLED_PROFILES
REPORT_NOW = "2026-10-08T12:00:00+00:00"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    names = (*THIRD_PARTY_HTTP_LOGGERS, *QUIET_LOGGERS, "hl7bridge")
    saved_levels = {n: logging.getLogger(n).level for n in names}
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch) -> None:
    for name in (
        "HL7BRIDGE_CONFIG",
        "HL7BRIDGE_DB",
        "HL7BRIDGE_FHIR_BASE",
        "HL7BRIDGE_FHIR_APIKEY",
        "HL7BRIDGE_FHIR_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sample_db(tmp_path) -> Path:
    """A store holding the twelve bundled samples."""
    db = tmp_path / "samples.db"
    from hl7bridge.config import HospitalRegistry
    from hl7bridge.ingest import IngestPipeline

    conn = connect(db)
    pipeline = IngestPipeline(HospitalRegistry.load_dir(CONFIG_DIR), conn)
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        pipeline.handle_bytes(path.read_bytes())
    conn.close()
    return db


def _patch_client(monkeypatch, handler) -> None:
    def factory(base_url: str, **kwargs):
        kwargs.pop("transport", None)
        return HapiClient(base_url, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli, "HapiClient", factory)


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


# -- layout and help ----------------------------------------------------------------


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"hl7bridge {__version__}"


def test_version_is_0_3(capsys):
    assert __version__.startswith("0.3.")


def test_top_help_groups_every_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for group in cli.GROUPS:
        assert f"\n{group}\n" in out
    for command in cli.COMMANDS:
        assert f"  {command.name} " in out, command.name
    assert "exit codes:" in out
    for old in ("serve", "fhir-sync", "fhir-probe", "load-studies", "load-outbound"):
        assert f"  {old} " not in out, "deprecated names stay out of the help"


@pytest.mark.parametrize("command", cli.COMMANDS, ids=lambda c: c.name)
def test_every_command_has_examples_in_its_help(capsys, command):
    with pytest.raises(SystemExit) as excinfo:
        cli.main([*command.name.split(), "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "examples:" in out
    assert f"hl7bridge {command.name}" in out.split("examples:", 1)[1]


@pytest.mark.parametrize("command", cli.COMMANDS, ids=lambda c: c.name)
def test_every_example_parses(command):
    parser = cli.build_parser()
    for example in command.examples:
        argv = example.split()[1:]
        args = parser.parse_args(argv)
        assert args.command == command.name


def test_report_commands_take_json():
    parser = cli.build_parser()
    for name in ("reconcile", "ack-report", "summary", "lint", "doctor"):
        extra = ["--month", "2026-10"] if name == "tat" else []
        assert parser.parse_args([*name.split(), "--json", *extra]).json is True
    assert parser.parse_args(["tat", "--month", "2026-10", "--json"]).json is True
    assert parser.parse_args(["profile", "show", "--json"]).json is True


def test_global_flags_work_before_or_after_the_command():
    parser = cli.build_parser()
    before = parser.parse_args(["-q", "--debug", "summary"])
    after = parser.parse_args(["summary", "-q", "--debug"])
    assert before.quiet and before.debug and after.quiet and after.debug
    assert parser.parse_args(["-vv", "summary"]).verbose == 2


def test_usage_error_from_argparse_exits_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["tat"])  # --month is required
    assert excinfo.value.code == EXIT_USAGE


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (["serve"], "listen"),
        (["fhir-sync"], "fhir sync"),
        (["fhir-probe"], "fhir probe"),
        (["load-studies", "--dicom", "x"], "load studies"),
        (["load-outbound", "--path", "x"], "load outbound"),
    ],
)
def test_old_names_still_parse(old, new):
    args = cli.build_parser().parse_args(old)
    assert args.command == new


def test_old_name_warns_and_runs(tmp_path, capsys, sample_db):
    path = tmp_path / "out.jsonl"
    line = {
        "hospital_id": "HOSPA",
        "control_id": "R1",
        "accession": "SACCA0001",
        "sent_at_utc": "2026-10-07T12:00:00+00:00",
    }
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    rc = cli.main(["load-outbound", "--db", str(sample_db), "--path", str(path)])
    err = capsys.readouterr().err
    assert rc == EXIT_OK
    assert "warning: 'load-outbound' is now 'hl7bridge load outbound'" in err
    assert "0.4.0" in err


# -- defaults: profiles and database ------------------------------------------------


def test_profiles_default_to_bundled(tmp_path):
    resolved = resolve_profiles(None, cwd=tmp_path)
    assert resolved.path == BUNDLED_PROFILES
    assert resolved.source == "bundled"


def test_profiles_prefer_local_folder_then_env_then_flag(tmp_path, monkeypatch):
    local = tmp_path / "hl7bridge" / "profiles"
    local.mkdir(parents=True)
    assert resolve_profiles(None, cwd=tmp_path).path == local
    monkeypatch.setenv("HL7BRIDGE_CONFIG", str(tmp_path / "env"))
    assert resolve_profiles(None, cwd=tmp_path).path == tmp_path / "env"
    assert resolve_profiles(None, cwd=tmp_path).source == "HL7BRIDGE_CONFIG"
    assert resolve_profiles("flagdir", cwd=tmp_path).path == Path("flagdir")
    assert resolve_profiles("flagdir", cwd=tmp_path).source == "--config"


def test_db_default_env_and_flag(monkeypatch):
    assert resolve_db(None).path == Path("hl7bridge.db")
    monkeypatch.setenv("HL7BRIDGE_DB", "env.db")
    assert resolve_db(None).path == Path("env.db")
    assert resolve_db("flag.db").path == Path("flag.db")


def test_commands_say_which_profiles_and_database_they_used(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["reconcile", "--now", REPORT_NOW])
    err = capsys.readouterr().err
    assert rc == EXIT_OK
    assert f"using profiles: {BUNDLED_PROFILES} (bundled)" in err
    assert "using database: hl7bridge.db (default)" in err
    assert (tmp_path / "hl7bridge.db").exists()


def test_quiet_drops_the_using_lines(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["-q", "summary"]) == EXIT_OK
    assert "using" not in capsys.readouterr().err


def test_fhir_headers_come_from_the_environment(monkeypatch):
    assert cli._fhir_headers() == {}
    monkeypatch.setenv("HL7BRIDGE_FHIR_APIKEY", "k-123")
    monkeypatch.setenv("HL7BRIDGE_FHIR_TOKEN", "t-456")
    assert cli._fhir_headers() == {"apikey": "k-123", "Authorization": "Bearer t-456"}


def test_bearer_token_is_sent_and_never_printed(monkeypatch, capsys):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(401, text="no")

    monkeypatch.setenv("HL7BRIDGE_FHIR_TOKEN", "t0ken-SECRET")
    _patch_client(monkeypatch, handler)
    rc = cli.main(["fhir", "probe", "--fhir-base", "http://fhir.test/fhir"])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS
    assert seen == ["Bearer t0ken-SECRET"]
    assert "t0ken-SECRET" not in captured.out + captured.err
    assert "HL7BRIDGE_FHIR_TOKEN" in captured.err, "the 401 hint names the variable"


def test_fhir_base_comes_from_the_environment(monkeypatch, capsys):
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setenv("HL7BRIDGE_FHIR_BASE", "http://env.test/fhir")
    _patch_client(monkeypatch, handler)
    assert cli.main(["fhir", "probe"]) == EXIT_CONNECTION
    assert urls == ["http://env.test/fhir/metadata"]


# -- error messages (exact text) ------------------------------------------------------


def test_fhir_server_down_message(tmp_path, capsys, monkeypatch):
    _patch_client(monkeypatch, _refused)
    rc = cli.main(["fhir", "sync", "--db", str(tmp_path / "f.db")])
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert (
        "error: no answer from http://localhost:8080/fhir/metadata. Is the server running?"
        " Try: hl7bridge doctor\n"
    ) in err
    assert "Traceback" not in err


def test_fhir_server_not_ready_message(tmp_path, capsys, monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(503, text="booting"))
    rc = cli.main(["fhir", "sync", "--db", str(tmp_path / "f.db")])
    err = capsys.readouterr().err
    assert rc == EXIT_FINDINGS
    assert "error: FHIR server returned 503 for /fhir/metadata: the server is not ready" in err


def test_no_hl7_files_message(tmp_path, capsys):
    empty = tmp_path / "msgs"
    empty.mkdir()
    for argv in (["send", str(empty)], ["lint", str(empty)]):
        rc = cli.main(argv)
        assert rc == EXIT_USAGE
        assert f"error: no *.hl7 files under {empty}. Try: hl7bridge init\n" in (
            capsys.readouterr().err
        )


def test_missing_path_message(tmp_path, capsys):
    rc = cli.main(["lint", str(tmp_path / "nope")])
    assert rc == EXIT_USAGE
    assert f"error: {tmp_path / 'nope'} does not exist. Try: hl7bridge init" in (
        capsys.readouterr().err
    )


def test_send_needs_a_path(capsys):
    assert cli.main(["send"]) == EXIT_USAGE
    assert "error: send: give a file or folder of *.hl7 messages." in capsys.readouterr().err


def test_bad_now_message(tmp_path, capsys):
    rc = cli.main(["reconcile", "--db", str(tmp_path / "r.db"), "--now", "2026-10-08T12:00:00"])
    assert rc == EXIT_USAGE
    assert (
        "error: --now '2026-10-08T12:00:00' has no UTC offset. Try --now 2026-10-08T12:00:00+00:00"
    ) in capsys.readouterr().err


def test_profile_unknown_key_message_through_the_cli(tmp_path, capsys):
    folder = tmp_path / "profiles"
    folder.mkdir()
    text = (BUNDLED_PROFILES / "hospb.yaml").read_text(encoding="utf-8")
    (folder / "hospb.yaml").write_text(
        text.replace("accession_field:", "acession_field:"), encoding="utf-8"
    )
    rc = cli.main(["lint", str(SAMPLES_DIR), "--config", str(folder)])
    assert rc == EXIT_USAGE
    assert (
        f"error: {folder / 'hospb.yaml'}: unknown key 'acession_field'."
        " Did you mean 'accession_field'?\n"
    ) in capsys.readouterr().err


def _old_schema_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (1)")
    conn.commit()
    conn.close()


def test_old_schema_message(tmp_path, capsys):
    db = tmp_path / "old.db"
    _old_schema_db(db)
    rc = cli.main(["summary", "--db", str(db)])
    assert rc == EXIT_USAGE
    assert (
        f"error: database {db} has schema version 1; this hl7bridge needs version 2."
        " There is no migration. Demo databases can be deleted and made again.\n"
    ) in capsys.readouterr().err
    # The old file was not changed.
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"schema_version"}


def test_not_a_database_message(tmp_path, capsys):
    db = tmp_path / "junk.db"
    db.write_bytes(b"this is not sqlite" * 100)
    rc = cli.main(["summary", "--db", str(db)])
    assert rc == EXIT_USAGE
    assert f"error: {db} is not an hl7bridge database (DatabaseError)." in capsys.readouterr().err


def test_missing_db_folder_message(tmp_path, capsys):
    db = tmp_path / "nofolder" / "x.db"
    rc = cli.main(["summary", "--db", str(db)])
    assert rc == EXIT_USAGE
    assert f"folder {db.parent} does not exist" in capsys.readouterr().err


def test_store_error_is_safe_to_print():
    assert StoreError.quotes_input is False


def test_debug_adds_frames_to_a_safe_error(tmp_path, capsys):
    db = tmp_path / "old.db"
    _old_schema_db(db)
    assert cli.main(["--debug", "summary", "--db", str(db)]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "error: database" in err
    assert "_init_schema" in err, "--debug prints the stack frames"


def test_keyboard_interrupt_exits_130(monkeypatch, capsys, tmp_path):
    def interrupted(_conn):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "message_counts", interrupted)
    assert cli.main(["summary", "--db", str(tmp_path / "k.db")]) == 130
    assert "interrupted" in capsys.readouterr().err


# -- listen ---------------------------------------------------------------------------


def _taken_port() -> tuple[socket.socket, int]:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: make the port truly taken
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    return holder, holder.getsockname()[1]


def test_listen_on_a_taken_port_fails_with_a_clear_message(tmp_path, capsys):
    holder, port = _taken_port()
    try:
        rc = cli.main(["listen", "--db", str(tmp_path / "b.db"), "--port", str(port)])
    finally:
        holder.close()
    captured = capsys.readouterr()
    assert rc == EXIT_USAGE
    assert (
        f"error: cannot listen on 127.0.0.1:{port}, the port is in use. Try --port {port + 1}.\n"
    ) in captured.err
    assert "MLLP listener on" not in captured.out, "no banner before a successful bind"


def _fake_serve(error: OSError):
    async def fake(host, port, handler, on_junk):
        raise error

    return fake


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            OSError(errno.EADDRINUSE, "in use"),
            "error: cannot listen on 127.0.0.1:2575, the port is in use. Try --port 2576.",
        ),
        (
            OSError(errno.EACCES, "denied"),
            "error: cannot listen on 127.0.0.1:2575, permission denied."
            " Ports below 1024 need admin rights. Try --port 2576.",
        ),
        (
            OSError(errno.EADDRNOTAVAIL, "not here"),
            "error: cannot listen on 127.0.0.1:2575, that address is not on this machine."
            " Try --host 127.0.0.1.",
        ),
        (
            socket.gaierror(socket.EAI_NONAME, "unknown"),
            "error: cannot listen on 127.0.0.1:2575, the host name does not resolve."
            " Try --host 127.0.0.1.",
        ),
        (
            OSError(errno.ENOMEM, "Out of memory"),
            "error: cannot listen on 127.0.0.1:2575: Out of memory.",
        ),
    ],
    ids=["in_use", "denied", "not_local", "no_such_host", "other"],
)
def test_listen_bind_errors_say_what_happened(tmp_path, capsys, monkeypatch, error, expected):
    monkeypatch.setattr(cli, "serve", _fake_serve(error))
    rc = cli.main(["listen", "--db", str(tmp_path / "b.db")])
    captured = capsys.readouterr()
    assert rc == EXIT_USAGE
    assert expected + "\n" in captured.err
    assert "MLLP listener on" not in captured.out


class _FakeServer:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.sockets = [type("S", (), {"getsockname": lambda self: ("127.0.0.1", 2575)})()]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def serve_forever(self):
        raise self.error


def test_listen_banner_only_after_bind_and_later_errors_are_not_bind_errors(
    tmp_path, capsys, monkeypatch
):
    async def fake(host, port, handler, on_junk):
        return _FakeServer(OSError(errno.ECONNABORTED, "Software caused connection abort"))

    monkeypatch.setattr(cli, "serve", fake)
    rc = cli.main(["listen", "--db", str(tmp_path / "b.db")])
    captured = capsys.readouterr()
    assert rc == EXIT_CONNECTION
    assert "MLLP listener on 127.0.0.1:2575" in captured.out
    assert "cannot listen" not in captured.err
    assert (
        "error: the listener on 127.0.0.1:2575 stopped: Software caused connection abort."
    ) in captured.err


def test_listen_ctrl_c_stops_cleanly(tmp_path, capsys, monkeypatch):
    async def fake(host, port, handler, on_junk):
        return _FakeServer(KeyboardInterrupt())

    monkeypatch.setattr(cli, "serve", fake)
    assert cli.main(["listen", "--db", str(tmp_path / "b.db")]) == EXIT_OK
    assert "listener stopped" in capsys.readouterr().out


def test_listen_warns_when_not_on_loopback(tmp_path, capsys, monkeypatch):
    async def fake(host, port, handler, on_junk):
        return _FakeServer(KeyboardInterrupt())

    monkeypatch.setattr(cli, "serve", fake)
    cli.main(["listen", "--host", "0.0.0.0", "--db", str(tmp_path / "b.db")])
    assert "MLLP has no encryption or authentication" in capsys.readouterr().err


def test_port_must_be_in_range(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["listen", "--port", "70000"])
    assert excinfo.value.code == EXIT_USAGE
    with pytest.raises(argparse.ArgumentTypeError):
        cli._port("abc")


# -- send -----------------------------------------------------------------------------


class _Listener:
    """A real loopback MLLP listener on a thread: the hl7bridge pipeline, or
    one that never answers (for the timeout case)."""

    def __init__(self, db: Path, *, silent: bool = False) -> None:
        from hl7bridge.config import HospitalRegistry
        from hl7bridge.ingest import IngestPipeline, serve

        self.loop = asyncio.new_event_loop()
        self.db = db
        self.silent = silent
        self.ready = threading.Event()
        self.port = 0
        self._serve = serve
        self._pipeline_cls = IngestPipeline
        self._registry = HospitalRegistry.load_dir(CONFIG_DIR)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def start():
            if self.silent:

                async def hold(reader, writer):
                    await reader.read()  # read and never answer
                    writer.close()

                server = await asyncio.start_server(hold, "127.0.0.1", 0)
            else:
                # The store is opened on this thread, the only one that uses it.
                pipeline = self._pipeline_cls(self._registry, connect(self.db))
                server = await self._serve(
                    "127.0.0.1", 0, pipeline.handle_frame, pipeline.handle_junk
                )
            self.server = server
            self.port = server.sockets[0].getsockname()[1]
            self.ready.set()

        self.loop.run_until_complete(start())
        self.loop.run_forever()

    def __enter__(self) -> _Listener:
        self.thread.start()
        assert self.ready.wait(10)
        return self

    def __exit__(self, *exc) -> None:
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def test_send_shows_ae_details_and_exits_1(tmp_path, capsys):
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(SAMPLES_DIR), "--port", str(listener.port)])
    out = capsys.readouterr().out
    assert rc == EXIT_FINDINGS
    assert (
        "11_broken_missing_accession.hl7: AE (application error, the sender should fix"
        " and resend)\n  MSA-3: missing_accession: ORC-3\n  quarantine reason:"
        " missing_accession\n"
    ) in out
    assert f"  next: hl7bridge lint {SAMPLES_DIR / '11_broken_missing_accession.hl7'}" in out
    assert "12_broken_bad_timestamp.hl7: AE" in out
    assert f"sent 12 message(s) from {SAMPLES_DIR}, got 12 ACK(s)\n  AA: 10\n  AE: 2\n" in out


def test_send_all_accepted_exits_0(tmp_path, capsys):
    one = SAMPLES_DIR / "01_hospa_adt_a04.hl7"
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(one), "--port", str(listener.port)])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert f"sent 1 message(s) from {one}, got 1 ACK(s)\n  AA: 1\n" in out


def test_send_converts_lf_line_ends_with_a_note(tmp_path, capsys):
    lf = tmp_path / "a.hl7"
    lf.write_bytes((SAMPLES_DIR / "01_hospa_adt_a04.hl7").read_bytes().replace(b"\r", b"\n"))
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(lf), "--port", str(listener.port)])
    out = capsys.readouterr().out
    assert rc == EXIT_OK, out
    assert "note: a.hl7 uses LF line ends; converted to CR for sending.\n" in out


def test_to_cr_handles_crlf_and_lf():
    assert cli._to_cr(b"A\r\nB\r\n") == (b"A\rB\r", True)
    assert cli._to_cr(b"A\nB\n") == (b"A\rB\r", True)
    assert cli._to_cr(b"A\rB\r") == (b"A\rB\r", False)


def test_send_no_ack_in_time_message(tmp_path, capsys):
    with _Listener(tmp_path / "s.db", silent=True) as listener:
        rc = cli.main(
            [
                "send",
                str(SAMPLES_DIR / "01_hospa_adt_a04.hl7"),
                "--port",
                str(listener.port),
                "--timeout",
                "0.3",
            ]
        )
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert (
        f"error: no ACK from 127.0.0.1:{listener.port} within 0.3 s."
        " The message may or may not have been processed.\n"
    ) in err


def test_send_connection_refused_message(capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rc = cli.main(["send", str(SAMPLES_DIR), "--port", str(port)])
    assert rc == EXIT_CONNECTION
    assert (
        f"error: cannot connect to 127.0.0.1:{port}: connection refused."
        " Is the listener running? Try: hl7bridge listen\n"
    ) in capsys.readouterr().err


def test_read_ack_shows_err_segment():
    raw = (
        b"MSH|^~\\&|A|B|C|D|20261007||ACK|1|T|2.5\rMSA|AR|X|rejected\rERR|||207^Application error\r"
    )
    view = cli._read_ack(raw)
    assert (view.code, view.text, view.err) == ("AR", "rejected", "207")


def test_send_frames_are_plain_mllp():
    assert encode_mllp(b"x") == b"\x0bx\x1c\r"


# -- --json -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["summary"],
        ["reconcile", "--now", REPORT_NOW],
        ["ack-report", "--now", REPORT_NOW],
        ["tat", "--month", "2026-10"],
    ],
    ids=["summary", "reconcile", "ack-report", "tat"],
)
def test_report_json_is_valid_and_stdout_only(sample_db, capsys, argv):
    rc = cli.main([*argv, "--db", str(sample_db), "--json"])
    captured = capsys.readouterr()
    assert rc == EXIT_OK
    data = json.loads(captured.out)
    assert data
    assert "using database" in captured.err, "status lines stay on stderr"


def test_summary_json_counts(sample_db, capsys):
    cli.main(["summary", "--db", str(sample_db), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["quarantine"] == {"bad_timestamp": 1, "missing_accession": 1}
    assert data["quarantine_total"] == 2
    assert data["messages"]["ORM^O01"] == 3


def test_tat_json_has_group_keys_and_breaches(sample_db, capsys):
    cli.main(["tat", "--db", str(sample_db), "--month", "2026-10", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"HOSPA", "HOSPB", "HOSPC"}
    assert all("breaches" in v and "groups" in v for v in data.values())


def test_lint_json(capsys):
    rc = cli.main(["lint", str(SAMPLES_DIR), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == EXIT_FINDINGS
    assert data["files_checked"] == 12
    assert data["error_count"] == 2
    assert {f["rule_id"] for f in data["findings"]} == {"HL7-010", "HL7-014"}


# -- init ---------------------------------------------------------------------------------


def test_init_copies_profiles_and_samples_byte_for_byte(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "wrote 3 profiles" in out and "wrote 12 sample messages" in out
    for src in BUNDLED_PROFILES.glob("*.yaml"):
        assert (tmp_path / "hl7bridge" / "profiles" / src.name).read_bytes() == src.read_bytes()
    for src in SAMPLES_DIR.glob("*.hl7"):
        copied = tmp_path / "hl7bridge" / "samples" / src.name
        assert copied.read_bytes() == src.read_bytes()
        assert b"\n" not in copied.read_bytes(), "segments end with a bare CR"
    # The default lookup now finds the copied profiles.
    assert resolve_profiles(None).source == "./hl7bridge/profiles"


def test_init_never_overwrites_without_force(tmp_path, capsys):
    dest = tmp_path / "site"
    assert cli.main(["init", str(dest)]) == EXIT_OK
    edited = dest / "profiles" / "hospa.yaml"
    edited.write_text("# my edits\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["init", str(dest)]) == EXIT_USAGE
    assert "Nothing was written. Pass --force" in capsys.readouterr().err
    assert edited.read_text(encoding="utf-8") == "# my edits\n"
    assert cli.main(["init", str(dest), "--force"]) == EXIT_OK
    assert edited.read_bytes() == (BUNDLED_PROFILES / "hospa.yaml").read_bytes()


def test_init_next_steps_name_the_folder(tmp_path, capsys):
    dest = tmp_path / "site"
    cli.main(["init", str(dest)])
    out = capsys.readouterr().out
    assert f"hl7bridge profile check --config {dest / 'profiles'}" in out


# -- profile show / check ----------------------------------------------------------------


def test_profile_show_fills_in_defaults(capsys):
    assert cli.main(["profile", "show", "HOSPB"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "# HOSPB: effective values, defaults filled in" in out
    assert "order_without_image_hours_by_modality: {}" in out, "a default made visible"
    assert "accession_field: ORC-3" in out


def test_profile_show_json_round_trips_through_the_loader(tmp_path, capsys):
    import yaml

    from hl7bridge.config import load_hospital_config

    cli.main(["profile", "show", "HOSPA", "--json"])
    data = json.loads(capsys.readouterr().out)["HOSPA"]
    path = tmp_path / "a.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_hospital_config(path) == load_hospital_config(BUNDLED_PROFILES / "hospa.yaml")


def test_profile_show_unknown_hospital(capsys):
    assert cli.main(["profile", "show", "HOSPZ"]) == EXIT_USAGE
    assert "error: no profile with hospital_id 'HOSPZ'. Known: HOSPA, HOSPB, HOSPC." in (
        capsys.readouterr().err
    )


def test_profile_check_reports_every_file(tmp_path, capsys):
    folder = tmp_path / "p"
    folder.mkdir()
    for src in BUNDLED_PROFILES.glob("*.yaml"):
        (folder / src.name).write_bytes(src.read_bytes())
    (folder / "hospb.yaml").write_text(
        (BUNDLED_PROFILES / "hospb.yaml")
        .read_text(encoding="utf-8")
        .replace("timezone:", "time_zone:"),
        encoding="utf-8",
    )
    rc = cli.main(["profile", "check", str(folder)])
    out = capsys.readouterr().out
    assert rc == EXIT_FINDINGS
    assert "OK   hospa.yaml (HOSPA)" in out
    assert "FAIL hospb.yaml:" in out and "Did you mean 'timezone'?" in out
    assert "OK   hospc.yaml (HOSPC)" in out
    assert f"2 of 3 OK in {folder}" in out


def test_profile_check_finds_duplicates_across_files(tmp_path, capsys):
    folder = tmp_path / "p"
    folder.mkdir()
    src = (BUNDLED_PROFILES / "hospa.yaml").read_bytes()
    (folder / "a.yaml").write_bytes(src)
    (folder / "b.yaml").write_bytes(src)
    assert cli.main(["profile", "check", str(folder), "--json"]) == EXIT_FINDINGS
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["files"][-1]["name"] == "(all profiles)"
    assert "duplicate hospital_id" in data["files"][-1]["problem"]


def test_profile_check_bundled_is_clean(capsys):
    assert cli.main(["profile", "check"]) == EXIT_OK


# -- doctor -------------------------------------------------------------------------------


def test_doctor_all_ok(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, lambda r: httpx.Response(200, json={}))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rc = cli.main(["doctor", "--port", str(port)])
    out = capsys.readouterr().out
    assert rc == EXIT_OK, out
    for name in (
        "python",
        "hl7bridge",
        "profiles",
        "time zones",
        "database",
        "listen port",
        "fhir",
    ):
        assert f"[OK] {name}:" in out, name
    assert not (tmp_path / "hl7bridge.db").exists(), "doctor never creates the database"
    assert out.rstrip().endswith("all checks passed")


def test_doctor_reports_an_old_database_with_the_fix(tmp_path, capsys, monkeypatch):
    db = tmp_path / "old.db"
    _old_schema_db(db)
    _patch_client(monkeypatch, _refused)
    rc = cli.main(["doctor", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == EXIT_FINDINGS
    assert (
        f"[FAIL] database: {db} (--db) has schema version 1; this hl7bridge needs version 2"
        " Fix: there is no migration. Delete it if it is a demo database,"
        " or pass --db with a new path."
    ) in out
    assert "[WARN] fhir: no answer from http://localhost:8080/fhir/metadata" in out


def test_doctor_flags_a_taken_port_and_bad_profiles(tmp_path, capsys, monkeypatch):
    holder, port = _taken_port()
    _patch_client(monkeypatch, _refused)
    try:
        rc = cli.main(["doctor", "--port", str(port), "--config", str(tmp_path / "none"), "--json"])
    finally:
        holder.close()
    data = json.loads(capsys.readouterr().out)
    by_name = {c["name"]: c for c in data["checks"]}
    assert rc == EXIT_FINDINGS and data["ok"] is False
    assert by_name["listen port"]["status"] == "WARN"
    assert by_name["profiles"]["status"] == "FAIL"
    assert "does not exist" in by_name["profiles"]["detail"]


def test_doctor_named_fhir_server_down_is_a_failure(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _refused)
    rc = cli.main(["doctor", "--fhir-base", "http://fhir.test/fhir"])
    out = capsys.readouterr().out
    assert rc == EXIT_FINDINGS
    assert "[FAIL] fhir: no answer from http://fhir.test/fhir/metadata" in out


def test_doctor_database_states(tmp_path):
    from hl7bridge.store.db import inspect_db

    assert inspect_db(tmp_path / "missing.db") == ("missing", None)
    good = tmp_path / "good.db"
    connect(good).close()
    assert inspect_db(good) == ("ok", 2)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"x" * 4096)
    assert inspect_db(junk)[0] == "not_a_database"
    empty_sqlite = tmp_path / "empty.db"
    sqlite3.connect(empty_sqlite).execute("CREATE TABLE t (x)").connection.close()
    assert inspect_db(empty_sqlite) == ("no_schema", None)


def test_doctor_prints_the_fhir_problem_once(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _refused)
    cli.main(["doctor"])
    captured = capsys.readouterr()
    assert "FHIR /metadata probe failed" not in captured.err
    assert captured.out.count("no answer from") == 1
    assert logging.getLogger("hl7bridge.fhir.client").level == logging.NOTSET
