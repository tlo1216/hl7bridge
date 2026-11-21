"""MLLP over TLS: handshake, mutual TLS, and the refusals.

Certificates are made at test time with a throwaway CA by the ``openssl``
command line tool (the ``cryptography`` package is not a dependency). The
tests that need a certificate (they use the ``certs`` fixture) skip, with the
reason, when the tool is not installed; HL7BRIDGE_TLS_REQUIRED=1 (set in CI)
makes that a failure instead. Tests that need none always run.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import math
import os
import shutil
import signal
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import ClassVar

import pytest

import hl7bridge
from hl7bridge import cli
from hl7bridge.errors import (
    EXIT_CONNECTION,
    EXIT_OK,
    EXIT_USAGE,
    BridgeError,
    ConnectError,
    TlsConfigError,
    TlsHandshakeError,
)
from hl7bridge.ingest import mllp
from hl7bridge.ingest.mllp import (
    Frame,
    JunkBytes,
    client_ssl_context,
    decode_frames,
    handshake_failures,
    send_messages,
    serve,
    server_ssl_context,
)
from hl7bridge.logsafety import NOTICE
from hl7bridge.profiles import SAMPLES_DIR

_FALLBACKS = (
    "C:/Program Files/Git/mingw64/bin/openssl.exe",
    "C:/Program Files/Git/usr/bin/openssl.exe",
)


def _find_openssl() -> str | None:
    found = shutil.which("openssl")
    if found:
        return found
    return next((p for p in _FALLBACKS if Path(p).is_file()), None)


OPENSSL = _find_openssl()
REQUIRED = os.environ.get("HL7BRIDGE_TLS_REQUIRED") == "1"


def _need_openssl() -> None:
    """Called by ``certs`` (module scope), so without openssl the tests that
    need a certificate skip instead of erroring in the fixture."""
    if OPENSSL is None:
        message = "the openssl command line tool is not installed; cannot make test certs"
        if REQUIRED:
            pytest.fail(f"HL7BRIDGE_TLS_REQUIRED=1 but {message}")
        pytest.skip(message)


@pytest.fixture(autouse=True)
def _fresh_handshake_state():
    mllp.reset_handshake_state()


CA_CNF = """[ca]
default_ca = CA_default
[CA_default]
database = index.txt
new_certs_dir = .
serial = serial
default_md = sha256
policy = policy_any
unique_subject = no
copy_extensions = none
[policy_any]
commonName = supplied
[req]
distinguished_name = dn
prompt = no
[dn]
CN = x
"""

MSG = (SAMPLES_DIR / "01_hospa_adt_a04.hl7").read_bytes().replace(b"\n", b"\r")
ACK = b"MSH|^~\\&|R|F|S|F|20261007||ACK|1|T|2.5\rMSA|AA|1\r"
EC = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")


def _run(cwd: Path, *args: str) -> None:
    assert OPENSSL is not None
    subprocess.run([OPENSSL, *args], cwd=cwd, check=True, capture_output=True)


class Pki:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._dirs: dict[str, Path] = {}

    def ca(self, name: str) -> Path:
        d = self.root / name
        d.mkdir()
        (d / "openssl.cnf").write_text(CA_CNF)
        (d / "index.txt").write_text("")
        (d / "serial").write_text("01\n")
        _run(
            d,
            "req",
            "-x509",
            *EC,
            "-keyout",
            "ca.key",
            "-out",
            "ca.pem",
            "-days",
            "2",
            "-subj",
            f"/CN=test {name}",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        )
        self._dirs[name] = d
        return d / "ca.pem"

    def leaf(
        self,
        ca: str,
        name: str,
        san: str,
        *,
        usage: str = "serverAuth",
        start: str = "20200101000000Z",
        end: str = "20991231000000Z",
    ) -> tuple[str, str]:
        d = self._dirs[ca]
        ext = d / f"{name}.ext"
        ext.write_text(
            f"subjectAltName={san}\nextendedKeyUsage={usage}\nbasicConstraints=CA:FALSE\n"
        )
        _run(
            d,
            "req",
            "-new",
            *EC,
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.csr",
            "-subj",
            f"/CN={name}",
            "-config",
            "openssl.cnf",
        )
        _run(
            d,
            "ca",
            "-batch",
            "-config",
            "openssl.cnf",
            "-in",
            f"{name}.csr",
            "-out",
            f"{name}.pem",
            "-notext",
            "-extfile",
            ext.name,
            "-startdate",
            start,
            "-enddate",
            end,
            "-keyfile",
            "ca.key",
            "-cert",
            "ca.pem",
        )
        return str(d / f"{name}.pem"), str(d / f"{name}.key")


@pytest.fixture(scope="module")
def certs(tmp_path_factory) -> dict[str, str]:
    _need_openssl()
    pki = Pki(tmp_path_factory.mktemp("pki"))
    ca = pki.ca("good")
    other = pki.ca("other")
    server = pki.leaf("good", "server", "IP:127.0.0.1,DNS:localhost")
    client = pki.leaf("good", "client", "DNS:client.test", usage="clientAuth")
    expired = pki.leaf(
        "good", "expired", "IP:127.0.0.1", start="20200101000000Z", end="20200102000000Z"
    )
    wrong_name = pki.leaf("good", "wrongname", "DNS:other.example")
    stranger = pki.leaf("other", "stranger", "DNS:client.test", usage="clientAuth")
    server_only = pki.leaf("good", "serveronly", "DNS:client.test", usage="serverAuth")
    return {
        "ca": str(ca),
        "other_ca": str(other),
        "server_cert": server[0],
        "server_key": server[1],
        "client_cert": client[0],
        "client_key": client[1],
        "expired_cert": expired[0],
        "expired_key": expired[1],
        "wrongname_cert": wrong_name[0],
        "wrongname_key": wrong_name[1],
        "stranger_cert": stranger[0],
        "stranger_key": stranger[1],
        "serveronly_cert": server_only[0],
        "serveronly_key": server_only[1],
    }


class TlsServer:
    """An MLLP listener on a thread that answers every frame with AA."""

    def __init__(
        self,
        ctx: ssl.SSLContext | None,
        drop_at: int | None = None,
        *,
        error: type[BaseException] = ConnectionResetError,
        delay_s: float = 0.0,
    ) -> None:
        self.ctx = ctx
        self.drop_at = drop_at
        self.error = error
        self.delay_s = delay_s
        self.junk: list[bytes] = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.port = 0
        self.received: list[bytes] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def handle(payload: bytes) -> bytes:
            self.received.append(payload)
            if self.drop_at is not None and len(self.received) >= self.drop_at:
                raise self.error  # the connection ends with no ACK
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            return ACK

        async def junk(data: bytes) -> None:
            self.junk.append(data)

        async def start() -> None:
            self.server = await serve("127.0.0.1", 0, handle, junk, ssl_context=self.ctx)
            self.port = self.server.sockets[0].getsockname()[1]
            self.ready.set()

        self.loop.run_until_complete(start())
        self.loop.run_forever()

    def __enter__(self) -> TlsServer:
        self.thread.start()
        assert self.ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        async def shutdown() -> None:
            self.server.close()
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current]
            for task in pending:  # connections still open: end them cleanly
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


def _send(port: int, ctx: ssl.SSLContext | None, host: str = "127.0.0.1") -> list[bytes]:
    return asyncio.run(send_messages(host, port, [MSG], ack_timeout_s=3.0, ssl_context=ctx))


REFUSED = (TlsHandshakeError, ssl.SSLError, ConnectionError)
PROMPT_S = 2.0  # _send waits up to 3 s; a refusal must come well before that


def _refused(port: int, ctx: ssl.SSLContext | None) -> BaseException:
    """The sender is refused, and promptly: a stall that ends in the timeout
    is a different failure and must not pass for a refusal."""
    start = time.monotonic()
    with pytest.raises(REFUSED) as exc:
        _send(port, ctx)
    assert time.monotonic() - start < PROMPT_S
    cause = getattr(exc.value, "cause", exc.value)
    assert not isinstance(cause, TimeoutError), "the handshake stalled instead of being refused"
    return exc.value


def test_tls_handshake_message_and_ack(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx) as srv:
        acks = _send(srv.port, client_ssl_context(certs["ca"]))
    assert acks == [ACK]
    assert srv.received == [MSG]


def test_contexts_are_tls_1_2_or_newer_and_verify(certs):
    ctx = client_ssl_context(certs["ca"])
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    server = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    assert server.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert server.verify_mode == ssl.CERT_REQUIRED


def test_mutual_tls_accepts_a_client_with_a_certificate(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    client_ctx = client_ssl_context(certs["ca"], certs["client_cert"], certs["client_key"])
    with TlsServer(server_ctx) as srv:
        assert _send(srv.port, client_ctx) == [ACK]


def test_mutual_tls_refuses_a_client_without_a_certificate(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    with TlsServer(server_ctx) as srv:
        _refused(srv.port, client_ssl_context(certs["ca"]))
    assert srv.received == []


def test_mutual_tls_refuses_a_client_certificate_from_another_ca(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    client_ctx = client_ssl_context(certs["ca"], certs["stranger_cert"], certs["stranger_key"])
    with TlsServer(server_ctx) as srv:
        _refused(srv.port, client_ctx)
    assert srv.received == []


def test_plain_client_is_refused_by_a_tls_server(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx) as srv:
        _refused(srv.port, None)
    assert srv.received == []


def test_tls_client_gets_no_handshake_from_a_plain_server(certs):
    # A plain listener waits for a frame and never answers a ClientHello, so
    # this ends in the handshake timeout (not a prompt refusal) and nothing
    # is written.
    with TlsServer(None) as srv, pytest.raises(TlsHandshakeError) as exc:
        _send(srv.port, client_ssl_context(certs["ca"]))
    assert isinstance(exc.value.cause, TimeoutError | ConnectionError)
    assert srv.received == []


def test_wrong_ca_is_refused(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx) as srv, pytest.raises(TlsHandshakeError) as exc:
        _send(srv.port, client_ssl_context(certs["other_ca"]))
    assert isinstance(exc.value.cause, ssl.SSLCertVerificationError)
    assert srv.received == []


def test_expired_certificate_is_refused(certs):
    server_ctx = server_ssl_context(certs["expired_cert"], certs["expired_key"])
    with TlsServer(server_ctx) as srv, pytest.raises(TlsHandshakeError, match="expired"):
        _send(srv.port, client_ssl_context(certs["ca"]))
    assert srv.received == []


def test_host_name_mismatch_is_refused(certs):
    server_ctx = server_ssl_context(certs["wrongname_cert"], certs["wrongname_key"])
    with (
        TlsServer(server_ctx) as srv,
        pytest.raises(TlsHandshakeError, match=r"127.0.0.1"),
    ):
        _send(srv.port, client_ssl_context(certs["ca"]))
    assert srv.received == []


def test_context_errors_name_the_file_and_not_the_key(certs, tmp_path):
    with pytest.raises(TlsConfigError, match="cannot read the TLS server certificate"):
        server_ssl_context(str(tmp_path / "none.pem"), certs["server_key"])
    # A certificate paired with the wrong key.
    with pytest.raises(TlsConfigError) as exc:
        server_ssl_context(certs["server_cert"], certs["client_key"])
    text = str(exc.value)
    assert "BEGIN" not in text and "PRIVATE" not in text
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    with pytest.raises(TlsConfigError, match="no usable PEM"):
        client_ssl_context(str(junk))
    with pytest.raises(TlsConfigError, match="needs both"):
        client_ssl_context(certs["ca"], certs["client_cert"], None)


def _send_args(port: int, *extra: str) -> list[str]:
    return ["send", str(SAMPLES_DIR / "01_hospa_adt_a04.hl7"), "--port", str(port), *extra]


def test_cli_send_over_tls(certs, capsys):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    with TlsServer(server_ctx) as srv:
        rc = cli.main(
            _send_args(
                srv.port,
                "--tls-ca",
                certs["ca"],
                "--tls-cert",
                certs["client_cert"],
                "--tls-key",
                certs["client_key"],
            )
        )
    captured = capsys.readouterr()
    assert rc == EXIT_OK, captured.err
    assert "got 1 ACK(s)" in captured.out


def test_cli_send_reports_a_refused_certificate_without_content(certs, capsys):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx) as srv:
        rc = cli.main(_send_args(srv.port, "--tls-ca", certs["other_ca"]))
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert "TLS certificate" in err and "was refused" in err
    assert "MSH" not in err and "PRIVATE" not in err


def test_cli_send_to_a_silent_listener_says_the_handshake_stalled(certs, capsys):
    # A plain listener never answers a ClientHello: a real stall, after the
    # TCP connection was made.
    with TlsServer(None) as srv:
        rc = cli.main(_send_args(srv.port, "--tls-ca", certs["ca"], "--timeout", "1"))
    assert rc == EXIT_CONNECTION
    err = capsys.readouterr().err
    assert "no TLS handshake" in err and "Nothing was sent" in err
    assert srv.received == []


def test_cli_flags_must_come_in_pairs(certs, capsys):
    rc = cli.main(_send_args(1, "--tls-cert", certs["client_cert"]))
    assert rc == EXIT_USAGE
    assert "--tls-cert and --tls-key go together" in capsys.readouterr().err
    rc = cli.main(["listen", "--tls-cert", certs["server_cert"]])
    assert rc == EXIT_USAGE
    assert "needs both --tls-cert and --tls-key" in capsys.readouterr().err
    rc = cli.main(["listen", "--tls-ca", certs["ca"]])
    assert rc == EXIT_USAGE


# -- Findings from the Nov 21 review ---------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ("--tls-ca", ""),
        ("--tls-cert", "", "--tls-key", "k.pem"),
        ("--tls-cert", "c.pem", "--tls-key", ""),
    ],
)
def test_empty_tls_values_are_a_usage_error_not_plaintext(extra, capsys):
    with TlsServer(None) as srv:
        rc = cli.main(_send_args(srv.port, *extra))
        assert srv.received == []
    assert rc == EXIT_USAGE
    assert "is empty" in capsys.readouterr().err


def test_listen_empty_tls_value_fails_before_the_store_is_made(certs, tmp_path, capsys):
    db = tmp_path / "never.db"
    for extra in (["--tls-cert", ""], ["--tls-cert", certs["server_cert"], "--tls-key", ""]):
        rc = cli.main(["listen", "--port", "0", "--db", str(db), *extra])
        assert rc == EXIT_USAGE
        assert "is empty" in capsys.readouterr().err
    rc = cli.main(["listen", "--port", "0", "--db", str(db), "--tls-cert", certs["server_cert"]])
    assert rc == EXIT_USAGE and not db.exists()
    capsys.readouterr()


def test_mid_stream_break_reports_acks_and_never_says_nothing_was_sent(certs, tmp_path, capsys):
    two = tmp_path / "two"
    two.mkdir()
    for name in ("a.hl7", "b.hl7"):
        (two / name).write_bytes((SAMPLES_DIR / "01_hospa_adt_a04.hl7").read_bytes())
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx, drop_at=2) as srv:
        rc = cli.main(["send", str(two), "--port", str(srv.port), "--tls-ca", certs["ca"]])
    captured = capsys.readouterr()
    assert rc == EXIT_CONNECTION
    assert "after 1 of 2 ACKs" in captured.err
    assert "may or may not have been processed" in captured.err
    assert "Nothing was sent" not in captured.err
    assert "no ACK" in captured.out  # per-message result for the second one


def test_system_store_context_is_built_like_the_ca_one():
    ctx = client_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.options & ssl.OP_NO_RENEGOTIATION


def test_sslkeylogfile_is_ignored_by_every_context(certs, tmp_path, monkeypatch):
    keylog = tmp_path / "keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    # Sanity check that the variable would have an effect on a default context.
    assert ssl.create_default_context().keylog_filename == str(keylog)
    keylog.unlink(missing_ok=True)  # making that context created the file
    contexts = [
        client_ssl_context(),
        client_ssl_context(certs["ca"]),
        server_ssl_context(certs["server_cert"], certs["server_key"]),
        server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"]),
    ]
    assert [c.keylog_filename for c in contexts] == [None] * 4
    assert not keylog.exists()


def test_cli_tls_flag_uses_the_system_store_and_refuses_a_private_ca(certs, capsys):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx) as srv:
        rc = cli.main(_send_args(srv.port, "--tls"))
        assert srv.received == []
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert "was refused" in err and "Nothing was sent" in err


def test_renegotiation_is_off_on_both_contexts(certs):
    server = server_ssl_context(certs["server_cert"], certs["server_key"])
    assert server.options & ssl.OP_NO_RENEGOTIATION
    assert client_ssl_context(certs["ca"]).options & ssl.OP_NO_RENEGOTIATION


def test_a_client_certificate_for_server_auth_only_is_refused(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    client_ctx = client_ssl_context(certs["ca"], certs["serveronly_cert"], certs["serveronly_key"])
    with TlsServer(server_ctx) as srv:
        _refused(srv.port, client_ctx)
    assert srv.received == []


def _old_tls_client() -> ssl.SSLContext:
    """A client that offers only TLS 1.0 and 1.1, or fails the test (CI) or
    skips (a laptop whose OpenSSL cannot offer them)."""
    old = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    old.check_hostname = False
    old.verify_mode = ssl.CERT_NONE
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            old.set_ciphers("DEFAULT:@SECLEVEL=0")
            old.minimum_version = ssl.TLSVersion.TLSv1
            old.maximum_version = ssl.TLSVersion.TLSv1_1
    except (ssl.SSLError, ValueError):
        message = "this OpenSSL cannot offer TLS 1.1 at all"
        if REQUIRED:
            pytest.fail(f"HL7BRIDGE_TLS_REQUIRED=1 but {message}")
        pytest.skip(message)
    return old


def test_tls_1_1_is_refused_by_the_listener(certs, caplog):
    # The refusal must come from the listener, not from the client failing
    # to start: the listener has to see and log a failed handshake.
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    old = _old_tls_client()
    with caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"), TlsServer(server_ctx) as srv:
        _refused(srv.port, old)
        assert _wait_for(lambda: handshake_failures() >= 1)
    assert srv.received == []
    assert "PROTOCOL" in caplog.text.upper() or "VERSION" in caplog.text.upper()


def _wait_for(check, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_failed_handshakes_are_logged_with_peer_and_counted(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    before = handshake_failures()
    with caplog.at_level(logging.INFO, logger="hl7bridge.ingest.mllp"):
        with TlsServer(server_ctx) as srv, pytest.raises(REFUSED):
            _send(srv.port, None)  # plain bytes sent to a TLS port
        assert _wait_for(lambda: handshake_failures() > before)
    text = caplog.text
    assert "tls handshake failed" in text and "peer=\"('127.0.0.1'" in text and "reason=" in text
    assert "MSH" not in text


def test_the_accepted_client_certificate_subject_is_logged(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    client_ctx = client_ssl_context(certs["ca"], certs["client_cert"], certs["client_key"])
    with (
        caplog.at_level(logging.INFO, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx) as srv,
    ):
        assert _send(srv.port, client_ctx) == [ACK]
    records = [r for r in caplog.records if "client certificate" in r.getMessage()]
    assert len(records) == 1
    # NOTICE sits above WARNING, so --quiet does not hide it.
    assert records[0].levelno == NOTICE > logging.WARNING
    text = records[0].getMessage()
    assert "commonName=client" in text and "DNS:client.test" in text
    assert "serial=" in text and "issuer=" in text and "test good" in text


def test_encrypted_and_unreadable_keys_get_their_own_messages(certs, tmp_path, monkeypatch):
    encrypted = tmp_path / "enc.key"
    _run(
        tmp_path,
        "pkcs8",
        "-topk8",
        "-in",
        certs["server_key"],
        "-out",
        str(encrypted),
        "-passout",
        "pass:secret",
        "-v2",
        "aes-256-cbc",
    )
    with pytest.raises(TlsConfigError, match="is encrypted") as exc:
        server_ssl_context(certs["server_cert"], str(encrypted))
    assert "secret" not in str(exc.value) and "BEGIN" not in str(exc.value)

    def denied(self, *args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", denied)
    with pytest.raises(TlsConfigError, match="permission denied"):
        server_ssl_context(certs["server_cert"], certs["server_key"])


def test_listener_error_during_a_connection_is_one_warning_not_a_traceback(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with caplog.at_level(logging.WARNING, logger="hl7bridge.ingest.mllp"):
        with TlsServer(server_ctx, drop_at=1) as srv, pytest.raises(REFUSED):
            _send(srv.port, client_ssl_context(certs["ca"]))
        assert _wait_for(lambda: "connection broke" in caplog.text)


def _spawn_listener(certs, db: Path, host: str, *extra: str) -> tuple[subprocess.Popen, int]:
    src = str(Path(hl7bridge.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src}
    code = "import sys; from hl7bridge.cli import main; sys.exit(main())"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            "listen",
            "--host",
            host,
            "--port",
            "0",
            "--db",
            str(db),
            "--tls-cert",
            certs["server_cert"],
            "--tls-key",
            certs["server_key"],
            *extra,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()  # printed once the socket is bound
        assert "MLLP over TLS listener on" in line, line
        port = int(line.split(" listener on ")[1].split(" ")[0].rsplit(":", 1)[1].rstrip(","))
    except BaseException:
        proc.kill()  # never leave the listener running when the start failed
        proc.communicate(timeout=10)
        raise
    return proc, port


def test_listen_over_tls_end_to_end_and_replay_over_tls(certs, tmp_path, capsys):
    db = tmp_path / "store.db"
    proc, port = _spawn_listener(certs, db, "127.0.0.1")
    try:
        rc = cli.main(_send_args(port, "--tls-ca", certs["ca"]))
        out = capsys.readouterr()
        assert rc == EXIT_OK, out.err
        assert "got 1 ACK(s)" in out.out
        rows = sqlite3.connect(db).execute("select count(*) from messages").fetchone()[0]
        assert rows == 1
    finally:
        proc.kill()
        proc.communicate(timeout=10)

    # Replay the stored message over TLS into a second listener (mutual TLS).
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    with TlsServer(server_ctx) as srv:
        rc = cli.main(
            [
                "replay",
                "--from",
                "store",
                "--db",
                str(db),
                "--port",
                str(srv.port),
                "--tls-ca",
                certs["ca"],
                "--tls-cert",
                certs["client_cert"],
                "--tls-key",
                certs["client_key"],
            ]
        )
        assert rc == EXIT_OK, capsys.readouterr().err
        assert len(srv.received) == 1


# -- Findings from the second Nov 21 review (A to K) -----------------------------------


def _many(tmp_path: Path, count: int) -> Path:
    folder = tmp_path / "many"
    folder.mkdir()
    for n in range(count):
        (folder / f"m{n}.hl7").write_bytes((SAMPLES_DIR / "01_hospa_adt_a04.hl7").read_bytes())
    return folder


def test_a_late_first_ack_is_one_unknown_and_the_rest_are_not_sent(certs, tmp_path, capsys):
    folder = _many(tmp_path, 3)
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx, delay_s=3.0) as srv:
        rc = cli.main(
            [
                "send",
                str(folder),
                "--port",
                str(srv.port),
                "--tls-ca",
                certs["ca"],
                "--timeout",
                "1",
            ]
        )
        received = len(srv.received)
    captured = capsys.readouterr()
    assert rc == EXIT_CONNECTION
    assert "no ACK from" in captured.err and "may or may not have been processed" in captured.err
    assert captured.out.count("no ACK (it may or may not have been processed)") == 1
    assert captured.out.count(": not sent") == 2
    assert received == 1


def test_an_ssl_error_mid_stream_reports_acks_and_marks_the_rest_not_sent(
    certs, tmp_path, capsys, monkeypatch
):
    real = mllp._read_one_frame
    calls = []

    async def flaky(reader):
        calls.append(1)
        if len(calls) == 2:
            raise ssl.SSLError("injected")
        return await real(reader)

    monkeypatch.setattr(mllp, "_read_one_frame", flaky)
    folder = _many(tmp_path, 3)
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx) as srv:
        rc = cli.main(["send", str(folder), "--port", str(srv.port), "--tls-ca", certs["ca"]])
    captured = capsys.readouterr()
    assert rc == EXIT_CONNECTION
    assert "broke (SSLError) after 1 of 3 ACKs" in captured.err
    assert "Nothing was sent" not in captured.err
    assert captured.out.count("no ACK (it may or may not") == 1
    assert captured.out.count(": not sent") == 1


@pytest.mark.parametrize("tls", [False, True], ids=["plain", "tls"])
@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(),
        OSError("Multiple exceptions: [Errno 111] connect call failed"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        TimeoutError(),
        socket.gaierror(socket.EAI_NONAME, "unknown"),
    ],
    ids=["refused", "multiple", "no_route", "timeout", "unknown_host"],
)
def test_every_failed_connect_is_one_never_connected_nothing_sent_error(
    error, tls, certs, capsys, monkeypatch
):
    async def fail(*_a, **_k):
        raise error

    monkeypatch.setattr(mllp.asyncio, "open_connection", fail)
    extra = ["--tls-ca", certs["ca"]] if tls else []
    rc = cli.main(_send_args(1, *extra))
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert "cannot connect to 127.0.0.1:1" in err
    assert "Never connected, nothing was sent." in err
    for wrong in ("broke", "after 0 of", "may or may not", "no TLS handshake"):
        assert wrong not in err


def test_send_messages_wraps_a_connect_failure_and_a_handshake_failure_apart(certs):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(ConnectError) as exc:
        _send(port, None)
    assert isinstance(exc.value.cause, ConnectionRefusedError)
    with TlsServer(None) as srv, pytest.raises(TlsHandshakeError):
        _send(srv.port, client_ssl_context(certs["ca"]))
    # Both are BridgeErrors with the connection exit code.
    assert issubclass(TlsHandshakeError, BridgeError) and issubclass(ConnectError, BridgeError)
    assert TlsHandshakeError.exit_code == EXIT_CONNECTION == ConnectError.exit_code


class _FakeWriter:
    def __init__(self, cert) -> None:
        self.cert = cert

    async def start_tls(self, *_a, **_k) -> None:
        return None

    def get_extra_info(self, name):
        return {"peername": ("10.1.1.1", 4000), "peercert": self.cert}.get(name)


def test_the_client_certificate_line_is_sanitized_and_always_written(caplog):
    forged = {
        "subject": ((("commonName", 'evil"\nserial=0x1 \x1b]0;pwn\x07'),),),
        "issuer": ((("commonName", "ca\r\nWARNING forged"),),),
        "serialNumber": "0A\x00B",
        "subjectAltName": (("DNS", "a.test\nb"), ("IP Address", "10.0.0.1")),
    }
    ident = mllp._identity(forged)
    assert all("\n" not in v and "\x1b" not in v and "\x00" not in v for v in ident.values())
    # A SAN-only certificate has no subject, and is still logged.
    san_only = {
        "subject": (),
        "issuer": ((("commonName", "ca"),),),
        "serialNumber": "01",
        "subjectAltName": (("DNS", "only.test"),),
    }
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"):
        assert asyncio.run(mllp._start_tls(_FakeWriter(forged), ctx))  # type: ignore[arg-type]
        assert asyncio.run(mllp._start_tls(_FakeWriter(san_only), ctx))  # type: ignore[arg-type]
        # A client without a certificate (no --tls-ca) logs no identity line.
        assert asyncio.run(mllp._start_tls(_FakeWriter(None), ctx))  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if "client certificate" in r.getMessage()]
    assert len(lines) == 2
    assert all("\n" not in line and "\x1b" not in line for line in lines)
    assert "subject=''" in lines[1] and "DNS:only.test" in lines[1] and "serial='01'" in lines[1]
    # The forged text stays inside one quoted value; it cannot add a field.
    assert lines[0].count(" serial=") == 1


def test_quiet_still_shows_the_security_notices():
    args = cli.build_parser().parse_args(["listen", "-q"])
    assert cli._log_level(args) <= NOTICE
    assert cli._log_level(args) > logging.WARNING  # warnings and below stay hidden


@pytest.mark.parametrize("command", ["send", "replay"])
@pytest.mark.parametrize("value", ["0", "-1", "-0.5", "nan", "inf", "-inf", "abc", ""])
def test_timeout_must_be_finite_and_above_zero(command, value, capsys):
    argv = [command, "x.hl7", "--timeout", value]
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2
    assert "--timeout" in capsys.readouterr().err


def test_a_zero_timeout_never_reaches_the_tls_code(certs, capsys):
    with pytest.raises(SystemExit):
        cli.main(_send_args(1, "--tls-ca", certs["ca"], "--timeout", "0"))
    assert "ValueError" not in capsys.readouterr().err
    assert math.isfinite(cli._seconds("0.25"))


def test_handshake_warnings_are_rate_limited_per_peer_with_a_summary(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(mllp, "_clock", lambda: now[0])
    err = ssl.SSLCertVerificationError(1, "bad")
    err.verify_message = "certificate has expired"
    err.reason = "CERTIFICATE_VERIFY_FAILED"
    with caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"):
        for port in range(5000, 5010):  # ten failures from one address, ten ports
            mllp._note_handshake_failure(("203.0.113.9", port), err)
        mllp._note_handshake_failure(("203.0.113.10", 1), err)  # another peer is its own window
        first = [r.getMessage() for r in caplog.records]
        assert len(first) == 2
        assert "certificate has expired" in first[0] and "CERTIFICATE_VERIFY_FAILED" in first[0]
        now[0] += 61
        mllp._note_handshake_failure(("203.0.113.9", 6000), err)
    lines = [r.getMessage() for r in caplog.records]
    assert any("9 more time(s)" in line and "203.0.113.9" in line for line in lines)
    assert handshake_failures() == 12
    caplog.clear()
    with caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"):
        mllp._note_handshake_failure(("203.0.113.9", 6001), err)
        mllp.log_handshake_total()
    text = caplog.text
    assert "1 more time(s)" in text and "in total: 13" in text


class _FakeServer:
    sockets: ClassVar[list] = []

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def serve_forever(self):
        mllp._note_handshake_failure(("203.0.113.9", 1), ssl.SSLError("x"))
        raise self.error


@pytest.mark.parametrize("how", ["ctrl_c", "error"])
def test_listen_logs_the_handshake_total_at_any_shutdown(
    how, tmp_path, caplog, capsys, monkeypatch
):
    error = KeyboardInterrupt() if how == "ctrl_c" else OSError("boom")

    async def fake(host, port, handler, on_junk, **kw):
        return _FakeServer(error)

    monkeypatch.setattr(cli, "serve", fake)
    with caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"):
        rc = cli.main(["listen", "--db", str(tmp_path / "b.db")])
    assert rc == (EXIT_OK if how == "ctrl_c" else EXIT_CONNECTION)
    assert "handshakes failed in total: 1" in caplog.text
    capsys.readouterr()


def test_a_non_loopback_tls_listener_without_a_ca_warns(certs, tmp_path, capsys, monkeypatch):
    async def fake(host, port, handler, on_junk, **kw):
        return _FakeServer(KeyboardInterrupt())

    monkeypatch.setattr(cli, "serve", fake)
    rc = cli.main(
        [
            "listen",
            "--host",
            "0.0.0.0",
            "--db",
            str(tmp_path / "w.db"),
            "--tls-cert",
            certs["server_cert"],
            "--tls-key",
            certs["server_key"],
        ]
    )
    assert rc == EXIT_OK
    assert "does not authenticate senders" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot deliver SIGTERM to a process")
def test_sigterm_stops_the_listener_and_logs_the_handshake_total(certs, tmp_path):
    proc, port = _spawn_listener(certs, tmp_path / "s.db", "127.0.0.1")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            raw.sendall(b"\x0bplain bytes to a TLS port\x1c\r")
            raw.settimeout(5)
            with contextlib.suppress(OSError):
                raw.recv(10)
        time.sleep(0.5)  # the listener has seen and counted the failure
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
    except BaseException:
        proc.kill()
        proc.communicate(timeout=10)
        raise
    assert proc.returncode == 0, err
    assert "listener stopped" in out
    assert "handshakes failed in total: 1" in err


class _FakeReader:
    """Hands out chunks, then raises an error, like a connection that breaks."""

    def __init__(self, *items: bytes | BaseException) -> None:
        self.items = list(items)

    async def read(self, _n: int) -> bytes:
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.parametrize("error", [ConnectionResetError(), ssl.SSLError("x"), TimeoutError()])
def test_bytes_of_a_partial_frame_are_not_dropped_when_the_connection_breaks(error):
    async def run() -> list[object]:
        seen: list[object] = []
        reader = _FakeReader(b"\x0bMSH|one\x1c\r\x0bMSH|par", b"tial", error)
        with pytest.raises(type(error)):
            async for item in decode_frames(reader):  # type: ignore[arg-type]
                seen.append(item)
        return seen

    seen = asyncio.run(run())
    assert seen == [Frame(b"MSH|one"), JunkBytes(b"\x0bMSH|partial")]


def test_a_reset_mid_frame_reaches_on_junk():
    with TlsServer(None) as srv:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as raw:
            raw.sendall(b"\x0bMSH|half a message")
            time.sleep(0.3)  # the listener has read it
            # RST instead of FIN: the listener's next read fails mid-frame.
            linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        assert _wait_for(lambda: srv.junk)
    assert srv.junk == [b"\x0bMSH|half a message"]


@pytest.mark.parametrize("error", [ssl.SSLError, TimeoutError])
def test_listener_survives_an_injected_error_and_names_only_its_class(certs, caplog, error):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with (
        caplog.at_level(logging.WARNING, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx, drop_at=1, error=error) as srv,
    ):
        with pytest.raises(REFUSED):
            _send(srv.port, client_ssl_context(certs["ca"]))
        assert _wait_for(lambda: f"error={error.__name__}" in caplog.text)
        srv.drop_at = None  # the next connection works: the listener is still up
        assert _send(srv.port, client_ssl_context(certs["ca"])) == [ACK]
    assert "Traceback" not in caplog.text


def test_a_peer_that_ignores_close_notify_does_not_hold_the_sender(certs):
    """The sender's shutdown wait is capped by --timeout, not asyncio's 30 s."""
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    done = threading.Event()

    def serve_once() -> None:
        conn, _ = listener.accept()
        with server_ctx.wrap_socket(conn, server_side=True) as tls:
            data = b""
            while not data.endswith(b"\x1c\r"):
                data += tls.recv(65536)
            tls.sendall(mllp.encode_mllp(ACK))
            done.wait(8)  # never answers the client's close_notify

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    try:
        start = time.monotonic()
        acks = asyncio.run(
            send_messages(
                "127.0.0.1",
                port,
                [MSG],
                ack_timeout_s=1.0,
                ssl_context=client_ssl_context(certs["ca"]),
            )
        )
        elapsed = time.monotonic() - start
    finally:
        done.set()
        thread.join(10)
        listener.close()
    assert acks == [ACK]
    assert elapsed < 5.0
