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
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    """A store holding the fifteen bundled samples."""
    db = tmp_path / "samples.db"
    from hl7bridge.config import HospitalRegistry
    from hl7bridge.ingest import IngestPipeline

    conn = connect(db)
    pipeline = IngestPipeline(HospitalRegistry.load_dir(CONFIG_DIR), conn)
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        pipeline.handle_bytes(path.read_bytes())
    conn.close()
    return db


def _empty_db(path: Path) -> Path:
    """An empty hl7bridge store: report commands never create one."""
    connect(path).close()
    return path


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


def test_version_is_0_5(capsys):
    assert __version__.startswith("0.5.")


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
    "old", ["serve", "fhir-sync", "fhir-probe", "load-studies", "load-outbound"]
)
def test_names_deprecated_in_0_3_are_gone(old, capsys):
    """0.3.x kept the old names with a warning; 0.4.0 removes them."""
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args([old])
    assert excinfo.value.code == EXIT_USAGE
    assert "invalid choice" in capsys.readouterr().err


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
    _empty_db(tmp_path / "hl7bridge.db")
    rc = cli.main(["reconcile", "--now", REPORT_NOW])
    err = capsys.readouterr().err
    assert rc == EXIT_OK
    assert f"using profiles: {BUNDLED_PROFILES} (bundled)" in err
    assert "using database: hl7bridge.db (default)" in err
    assert (tmp_path / "hl7bridge.db").exists()


def test_quiet_drops_the_using_lines(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _empty_db(tmp_path / "hl7bridge.db")
    assert cli.main(["-q", "summary"]) == EXIT_OK
    assert "using" not in capsys.readouterr().err


def test_fhir_headers_come_from_the_environment(monkeypatch):
    assert cli._fhir_headers() == {}
    monkeypatch.setenv("HL7BRIDGE_FHIR_APIKEY", "k-123")
    monkeypatch.setenv("HL7BRIDGE_FHIR_TOKEN", "t-456")
    assert cli._fhir_headers() == {"apikey": "k-123", "Authorization": "Bearer t-456"}


TOKEN = "t0ken-CANARY-7731"  # guardrails: allow (test canary, not a secret)
API_KEY = "k3y-CANARY-4420"  # guardrails: allow (test canary, not a secret)


@contextmanager
def _token_echoing_server(status: int) -> Iterator[tuple[str, list[str]]]:
    """A real HTTP server on 127.0.0.1 (httpx goes through httpcore, unlike
    MockTransport) that puts the Authorization and apikey values it gets in
    every place a server can: the reason phrase, a Location query, a header
    and an OperationOutcome body. Yields the base URL and the headers seen."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            auth = self.headers.get("Authorization", "")
            key = self.headers.get("apikey", "")
            seen.append(auth)
            echo = f"{auth} {key}"
            body = json.dumps(
                {
                    "resourceType": "OperationOutcome",
                    "issue": [{"code": echo, "diagnostics": echo}],
                }
            ).encode()
            self.send_response(status, f"Denied {echo}")
            self.send_header("Location", f"/fhir/metadata?seen={auth.replace(' ', '+')}")
            self.send_header("X-Echo", echo)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/fhir", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize(
    "argv",
    [["fhir", "probe"], ["doctor", "--json"], ["fhir", "sync"]],
    ids=["probe", "doctor", "sync"],
)
def test_bearer_token_is_sent_and_never_printed_or_logged(tmp_path, capsys, monkeypatch, argv):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("HL7BRIDGE_FHIR_TOKEN", TOKEN)
    monkeypatch.setenv("HL7BRIDGE_FHIR_APIKEY", API_KEY)
    monkeypatch.chdir(tmp_path)
    extra = ["--db", str(_empty_db(tmp_path / "s.db"))] if argv[-1] == "sync" else []
    real_configure = cli.configure_logging

    def configure_and_open_http_logs(level: int) -> None:
        # The CLI holds httpx and httpcore at WARNING. Open them to DEBUG, as
        # an integrator might, so their records reach the CLI's handler and
        # only the redaction stands between the server's echo and stderr.
        real_configure(level)
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.DEBUG)

    monkeypatch.setattr(cli, "configure_logging", configure_and_open_http_logs)
    with _token_echoing_server(401) as (base, seen):
        rc = cli.main(["-vv", *argv, *extra, "--fhir-base", base])
    captured = capsys.readouterr()
    everything = captured.out + captured.err
    assert rc == EXIT_FINDINGS
    assert seen and set(seen) == {f"Bearer {TOKEN}"}, "the token was sent"
    assert "HTTP Request: GET" in captured.err, "the HTTP client logged the request"
    assert "receive_response_headers.complete" in captured.err, "httpcore logged too"
    assert TOKEN not in everything
    assert API_KEY not in everything
    assert "CANARY" not in everything


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
    db = _empty_db(tmp_path / "f.db")
    rc = cli.main(["fhir", "sync", "--db", str(db), "--fhir-base", "http://localhost:8080/fhir"])
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert (
        "error: no answer from http://localhost:8080/fhir/metadata. Is the server running?"
        " Try: hl7bridge doctor\n"
    ) in err
    assert "Traceback" not in err


def test_fhir_server_not_ready_message(tmp_path, capsys, monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(503, text="booting"))
    db = _empty_db(tmp_path / "f.db")
    rc = cli.main(["fhir", "sync", "--db", str(db), "--fhir-base", "http://localhost:8080/fhir"])
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
        f"error: database {db} has schema version 1; this hl7bridge needs version 3."
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
    log = tmp_path / "out.jsonl"
    log.write_text("", encoding="utf-8")
    rc = cli.main(["load", "outbound", "--db", str(db), "--path", str(log)])
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
    assert cli.main(["summary", "--db", str(_empty_db(tmp_path / "k.db"))]) == 130
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
        # --host given explicitly: the default is tested on its own below, so a
        # wrong default fails that test instead of hanging this one.
        rc = cli.main(
            ["listen", "--db", str(tmp_path / "b.db"), "--host", "127.0.0.1", "--port", str(port)]
        )
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
    assert f"sent 15 message(s) from {SAMPLES_DIR}, got 15 ACK(s)\n  AA: 13\n  AE: 2\n" in out


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
    assert data["files_checked"] == 15
    assert data["error_count"] == 2
    assert {f["rule_id"] for f in data["findings"]} == {"HL7-010", "HL7-014"}


# -- init ---------------------------------------------------------------------------------


def test_init_copies_profiles_and_samples_byte_for_byte(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "wrote 3 profiles" in out and "wrote 15 sample messages" in out
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
        f"[FAIL] database: {db} (--db) has schema version 1; this hl7bridge needs version 3"
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
    assert inspect_db(good) == ("ok", 3)
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


# -- the store: report commands never create a database --------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["summary"],
        ["reconcile", "--now", REPORT_NOW],
        ["ack-report", "--now", REPORT_NOW],
        ["tat", "--month", "2026-10"],
        ["fhir", "sync", "--fhir-base", "http://fhir.test/fhir"],
    ],
    ids=["summary", "reconcile", "ack-report", "tat", "fhir-sync"],
)
@pytest.mark.parametrize("named", [False, True], ids=["default", "flag"])
def test_report_commands_refuse_a_missing_database(tmp_path, capsys, monkeypatch, argv, named):
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, lambda r: pytest.fail("no request without a database"))
    db = tmp_path / "missing.db" if named else Path("hl7bridge.db")
    rc = cli.main([*argv, *(["--db", str(db)] if named else [])])
    err = capsys.readouterr().err
    assert rc == EXIT_USAGE
    assert f"error: no database at {db}. Run hl7bridge listen or hl7bridge load first" in err
    assert list(tmp_path.iterdir()) == [], "nothing was created"


def test_a_database_of_another_program_is_refused_and_left_alone(tmp_path, capsys):
    db = tmp_path / "other-app.sqlite"
    other = sqlite3.connect(db)
    other.execute("CREATE TABLE notes (body TEXT)")
    other.commit()
    other.close()
    (tmp_path / "dicom").mkdir()
    for argv in (
        ["summary", "--db", str(db)],
        ["load", "studies", "--db", str(db), "--dicom", str(tmp_path / "dicom")],
    ):
        assert cli.main(argv) == EXIT_USAGE
        assert f"error: {db} is a SQLite database that hl7bridge did not create." in (
            capsys.readouterr().err
        )
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"notes"}


# -- FHIR: which server, and what is printed about it ------------------------------------


def test_fhir_sync_needs_a_named_server(tmp_path, capsys, monkeypatch):
    _patch_client(monkeypatch, lambda r: pytest.fail("sync must not pick a server itself"))
    db = _empty_db(tmp_path / "s.db")
    assert cli.main(["fhir", "sync", "--db", str(db)]) == EXIT_USAGE
    assert (
        "error: fhir sync writes to a FHIR server, so it needs one named."
        " Pass --fhir-base URL or set HL7BRIDGE_FHIR_BASE."
    ) in capsys.readouterr().err


def test_fhir_sync_takes_the_server_from_the_environment(tmp_path, capsys, monkeypatch):
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={})

    _patch_client(monkeypatch, handler)
    monkeypatch.setenv("HL7BRIDGE_FHIR_BASE", "http://env.test/fhir")
    assert cli.main(["fhir", "sync", "--db", str(_empty_db(tmp_path / "s.db"))]) == EXIT_OK
    assert urls == ["http://env.test/fhir/metadata"]


@pytest.mark.parametrize(
    "value",
    [
        "alice:s3cret-PW@127.0.0.1:9/fhir",
        "http:/alice:s3cret-PW@127.0.0.1/fhir",
        "https:alice:s3cret-PW@host/fhir",
        "ftp://alice:s3cret-PW@host/fhir",
        "http://alice:s3cret-PW@host:notaport/fhir",
        "http://host/fhir?token=s3cret-PW",
        "http:///alice:s3cret-PW@/fhir",
    ],
)
@pytest.mark.parametrize(
    "argv",
    [["fhir", "probe"], ["doctor"], ["doctor", "--json"], ["fhir", "sync"], ["demo"]],
    ids=["probe", "doctor", "doctor-json", "sync", "demo"],
)
@pytest.mark.parametrize("source", ["flag", "env"])
def test_a_bad_fhir_base_is_refused_without_echoing_it(
    tmp_path, capsys, monkeypatch, value, argv, source
):
    _patch_client(monkeypatch, lambda r: pytest.fail("no request for a bad URL"))
    db = _empty_db(tmp_path / "s.db")
    extra = ["--db", str(db)] if argv[-1] == "sync" else []
    if argv == ["demo"]:
        extra = ["--data-dir", str(tmp_path / "d")]
    if source == "env":
        if argv == ["demo"]:
            pytest.skip("the demo never reads the environment variable")
        monkeypatch.setenv("HL7BRIDGE_FHIR_BASE", value)
        rc = cli.main([*argv, *extra])
        named = "HL7BRIDGE_FHIR_BASE"
    else:
        rc = cli.main([*argv, *extra, "--fhir-base", value])
        named = "--fhir-base"
    captured = capsys.readouterr()
    assert rc == EXIT_USAGE
    assert f"error: {named} is not an http(s) URL with a host (value not shown)." in captured.err
    assert "s3cret-PW" not in captured.out + captured.err
    assert "alice" not in captured.out + captured.err
    assert not (tmp_path / "d").exists(), "the demo stopped before writing anything"


def test_the_bearer_token_goes_only_to_a_named_server(tmp_path, capsys, monkeypatch):
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("Authorization", "")))
        return httpx.Response(200, json={})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HL7BRIDGE_FHIR_TOKEN", "t0ken-SECRET")
    monkeypatch.setenv("HL7BRIDGE_FHIR_APIKEY", "k3y-SECRET")
    _patch_client(monkeypatch, handler)
    cli.main(["doctor"])
    cli.main(["fhir", "probe"])
    assert seen and all(auth == "" for _, auth in seen), "default server: no credentials"
    seen.clear()
    cli.main(["doctor", "--fhir-base", "http://127.0.0.1:9/fhir"])
    assert seen == [("http://127.0.0.1:9/fhir/metadata", "Bearer t0ken-SECRET")]
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err


def test_doctor_on_the_default_server_warns_for_any_answer(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, lambda r: httpx.Response(404, text="some other web app"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rc = cli.main(["doctor", "--port", str(port)])
    out = capsys.readouterr().out
    assert rc == EXIT_OK, out
    assert (
        "[WARN] fhir: http://localhost:8080/fhir/metadata answered HTTP 404;"
        " it may not be a FHIR server"
    ) in out


def test_doctor_on_a_named_server_fails_for_an_http_error(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, lambda r: httpx.Response(401))
    assert cli.main(["doctor", "--fhir-base", "http://fhir.test/fhir"]) == EXIT_FINDINGS
    assert "[FAIL] fhir: http://fhir.test/fhir/metadata answered HTTP 401" in (
        capsys.readouterr().out
    )


def test_credentials_over_plain_http_to_another_machine_warn(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HL7BRIDGE_FHIR_TOKEN", "t0ken-SECRET")
    _patch_client(monkeypatch, lambda r: httpx.Response(200, json={}))
    cli.main(["doctor", "--fhir-base", "http://fhir.test/fhir"])
    err = capsys.readouterr().err
    assert "warning: sending credentials to http://fhir.test/fhir over plain http" in err
    for base in ("https://fhir.test/fhir", "http://127.0.0.1:8080/fhir"):
        cli.main(["doctor", "--fhir-base", base])
        assert "over plain http" not in capsys.readouterr().err, base


def test_fhir_probe_strips_control_characters_from_server_text(capsys, monkeypatch):
    statement = {
        "resourceType": "CapabilityStatement",
        "software": {"name": "HAPI\x1b]0;owned\x07", "version": "8\x1b[2J"},
        "fhirVersion": "4.0.1\x9b",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json=statement)
        return httpx.Response(200, json={"total": 0})

    _patch_client(monkeypatch, handler)
    assert cli.main(["fhir", "probe", "--fhir-base", "http://fhir.test/fhir"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out
    assert "  software: HAPI?]0;owned? 8?[2J" in out


# -- send: what the receiver wrote is never printed raw ---------------------------------


class _AckingListener:
    """A loopback MLLP listener that answers every frame with one fixed ACK."""

    def __init__(self, ack: bytes) -> None:
        self.ack = ack
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def handle(reader, writer):
            buffer = b""
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\x1c\r" in buffer:
                    _frame, buffer = buffer.split(b"\x1c\r", 1)
                    writer.write(encode_mllp(self.ack))
                    await writer.drain()
            writer.close()

        async def start():
            self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self.ready.set()

        self.loop.run_until_complete(start())
        self.loop.run_forever()

    def __enter__(self) -> _AckingListener:
        self.thread.start()
        assert self.ready.wait(10)
        return self

    def __exit__(self, *exc) -> None:
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def _ack(code: str, msa3: str, err: str = "") -> bytes:
    segments = [
        "MSH|^~\\&|RCV|R|SND|S|20261007100000||ACK|A1|P|2.5",
        f"MSA|{code}|C1|{msa3}",
    ]
    if err:
        segments.append(f"ERR|||{err}")
    return ("\r".join(segments) + "\r").encode("utf-8")


def _one_message(tmp_path: Path) -> Path:
    msg = tmp_path / "a.hl7"
    msg.write_bytes(sorted(SAMPLES_DIR.glob("*.hl7"))[0].read_bytes())
    return msg


def test_send_strips_control_characters_from_the_receivers_ack(tmp_path, capsys):
    hostile = "\x1b]0;owned\x07\x1b[2Jlooks fine‮"
    msg = _one_message(tmp_path)
    with _AckingListener(_ack("AE", hostile, err=hostile)) as listener:
        rc = cli.main(["send", str(msg), "--port", str(listener.port)])
    out = capsys.readouterr().out
    assert rc == EXIT_FINDINGS
    for ch in ("\x1b", "\x07", "‮"):
        assert ch not in out
    assert "  MSA-3: ?]0;owned??[2Jlooks fine?" in out
    assert "  ERR: ?]0;owned??[2Jlooks fine?" in out
    assert "quarantine reason" not in out, "only hl7bridge's own reason codes"


def test_send_shows_a_quarantine_reason_only_for_known_codes(tmp_path, capsys):
    msg = _one_message(tmp_path)
    with _AckingListener(_ack("AE", "not a patient here: PID-3")) as listener:
        cli.main(["send", str(msg), "--port", str(listener.port)])
    out = capsys.readouterr().out
    assert "  MSA-3: not a patient here: PID-3" in out
    assert "quarantine reason" not in out


def test_send_never_prints_a_code_that_is_not_an_ack_code(tmp_path, capsys):
    msg = _one_message(tmp_path)
    with _AckingListener(_ack("\x1b[2J", "x")) as listener:
        cli.main(["send", str(msg), "--port", str(listener.port)])
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "a.hl7: ?? (not a known ACK code)" in out


# -- --json: an explicit list of fields per report ----------------------------------------


def test_report_json_fields_are_exactly_the_listed_ones(sample_db, tmp_path, capsys):
    from hl7bridge.config import HospitalRegistry
    from hl7bridge.ingest.loaders import load_outbound
    from hl7bridge.synth import generate

    conn = connect(sample_db)
    registry = HospitalRegistry.load_dir(CONFIG_DIR)
    generate(tmp_path / "synth", registry, seed=42)
    load_outbound(conn, registry, tmp_path / "synth" / "outbound_reports.jsonl")
    conn.execute(
        "INSERT INTO acks_in (hospital_id, ack_control_id, msa_code, msa_control_id, reason,"
        " received_at_utc) VALUES ('HOSPA', 'X1', 'AA', 'NEVER-SENT', 'free text', ?)",
        (REPORT_NOW,),
    )
    conn.commit()
    conn.close()

    cli.main(["reconcile", "--db", str(sample_db), "--now", REPORT_NOW, "--json"])
    rec = json.loads(capsys.readouterr().out)
    assert any(h["findings"] for h in rec.values())
    for hosp in rec.values():
        assert set(hosp) == {*cli.RECONCILE_FIELDS, "finding_count", "findings"}
        for finding in hosp["findings"]:
            assert set(finding) == set(cli.FINDING_FIELDS)

    cli.main(["ack-report", "--db", str(sample_db), "--now", REPORT_NOW, "--json"])
    ack = json.loads(capsys.readouterr().out)
    assert set(ack) == {
        "states",
        "by_hospital",
        "outcomes",
        "unknown_acks",
        "unconfigured_hospital_reports",
    }
    assert ack["outcomes"] and all(set(o) == set(cli.OUTCOME_FIELDS) for o in ack["outcomes"])
    assert ack["unknown_acks"]
    assert all(set(a) == set(cli.UNKNOWN_ACK_FIELDS) for a in ack["unknown_acks"])

    cli.main(["tat", "--db", str(sample_db), "--month", "2026-10", "--json"])
    tat = json.loads(capsys.readouterr().out)
    for hosp in tat.values():
        assert set(hosp) == {*cli.TAT_FIELDS, "measured", "groups"}
        assert all(set(m) == set(cli.MEASURED_FIELDS) for m in hosp["measured"])
        assert all(set(g) == set(cli.GROUP_FIELDS) for g in hosp["groups"].values())

    cli.main(["lint", str(SAMPLES_DIR), "--json"])
    lint = json.loads(capsys.readouterr().out)
    assert all(set(f) == {"file", *cli.LINT_FIELDS} for f in lint["findings"])


# -- user mistakes exit 2 ----------------------------------------------------------------


@pytest.mark.parametrize("month", ["garbage", "2026-13", "2026-00", "2026-1", "26-10"])
def test_tat_month_must_be_a_real_month(tmp_path, capsys, month):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["tat", "--db", str(_empty_db(tmp_path / "t.db")), "--month", month])
    assert excinfo.value.code == EXIT_USAGE
    assert "is not a month in the form YYYY-MM" in capsys.readouterr().err


def test_load_outbound_missing_file_is_a_usage_error(tmp_path, capsys):
    missing = tmp_path / "missing.jsonl"
    rc = cli.main(["load", "outbound", "--db", str(tmp_path / "x.db"), "--path", str(missing)])
    assert rc == EXIT_USAGE
    assert f"error: --path {missing} is not a file.\n" in capsys.readouterr().err
    assert not (tmp_path / "x.db").exists()


def test_load_studies_missing_folder_is_a_usage_error(tmp_path, capsys):
    missing = tmp_path / "nodicom"
    rc = cli.main(["load", "studies", "--db", str(tmp_path / "x.db"), "--dicom", str(missing)])
    assert rc == EXIT_USAGE
    assert f"error: --dicom {missing} is not a folder." in capsys.readouterr().err


def test_synth_into_a_file_is_a_usage_error(tmp_path, capsys):
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    assert cli.main(["synth", "--out", str(target)]) == EXIT_USAGE
    assert f"error: --out {target} exists and is not a folder." in capsys.readouterr().err


def test_init_into_a_file_is_a_usage_error(tmp_path, capsys):
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    assert cli.main(["init", str(target)]) == EXIT_USAGE
    assert f"error: {target} exists and is not a folder." in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "x"


def test_init_when_profiles_is_a_file_is_a_usage_error(tmp_path, capsys):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "profiles").write_text("x", encoding="utf-8")
    assert cli.main(["init", str(tmp_path / "site")]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert f"error: {tmp_path / 'site' / 'profiles'} exists and is not a folder." in err
    assert not (tmp_path / "site" / "samples").exists(), "nothing was written"


def test_file_system_errors_are_usage_errors_without_the_file_name(tmp_path, capsys, monkeypatch):
    def missing(_conn):
        raise FileNotFoundError(2, "No such file or directory", "2.25.1234567.dcm")

    monkeypatch.setattr(cli, "message_counts", missing)
    rc = cli.main(["summary", "--db", str(_empty_db(tmp_path / "x.db"))])
    err = capsys.readouterr().err
    assert rc == EXIT_USAGE
    assert "error: FileNotFoundError: No such file or directory. Check the paths you passed." in err
    assert "2.25.1234567" not in err
    assert "bug in hl7bridge" not in err


def test_lint_reports_a_file_that_is_not_utf8(tmp_path, capsys):
    (tmp_path / "bad.hl7").write_bytes(b"MSH|^~\\&|\xff\xfe\r")
    rc = cli.main(["lint", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == EXIT_FINDINGS
    assert [(f["file"], f["rule_id"]) for f in data["findings"]] == [("bad.hl7", "HL7-001")]


# -- defaults that keep things on this machine ------------------------------------------


@pytest.mark.parametrize("command", [["listen"], ["send", "x"], ["doctor"]])
def test_network_defaults_are_loopback(command):
    assert cli.build_parser().parse_args(command).host == "127.0.0.1"


def test_report_json_never_grows_with_the_report_dataclasses(tmp_path, capsys, monkeypatch):
    """A field added to a report later must not reach --json by accident."""
    from dataclasses import dataclass, field

    from hl7bridge.acks.tracker import AckReport

    @dataclass
    class Finding:
        category: str = "order_without_image"
        hospital_id: str = "HOSPA"
        accession: str | None = "ACC1"
        study_uid: str | None = None
        detail: str = "d"
        raw_message: str = "LEAK-CANARY"

    @dataclass
    class Reconcile:
        hospital_id: str = "HOSPA"
        display_name: str = "A"
        matched: int = 0
        pending_orders: int = 0
        pending_studies: int = 0
        findings: list[Finding] = field(default_factory=lambda: [Finding()])
        raw_rows: str = "LEAK-CANARY"

    @dataclass
    class Outcome:
        hospital_id: str = "HOSPA"
        control_id: str = "C1"
        accession: str = "ACC1"
        state: str = "DELIVERED"
        sent_at_utc: str = REPORT_NOW
        acked_at_utc: str | None = None
        reason: str | None = None
        duplicate_acks: int = 0
        conflicting_codes: tuple[str, ...] = ()
        raw_ack: str = "LEAK-CANARY"

    db = str(_empty_db(tmp_path / "j.db"))
    monkeypatch.setattr(cli, "reconcile", lambda conn, reg, now: {"HOSPA": Reconcile()})
    cli.main(["reconcile", "--db", db, "--now", REPORT_NOW, "--json"])
    monkeypatch.setattr(
        cli,
        "track_acks",
        lambda conn, reg, now: AckReport(outcomes=[Outcome()]),  # type: ignore[list-item]
    )
    cli.main(["ack-report", "--db", db, "--now", REPORT_NOW, "--json"])
    out = capsys.readouterr().out
    assert '"matched"' in out and '"control_id"' in out
    assert "LEAK-CANARY" not in out
