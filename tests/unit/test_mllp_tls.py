"""MLLP over TLS: handshake, mutual TLS, and the refusals.

Certificates are made at test time with a throwaway CA by the ``openssl``
command line tool (the ``cryptography`` package is not a dependency). The
tests skip, with the reason, when the tool is not installed;
HL7BRIDGE_TLS_REQUIRED=1 (set in CI) makes that a failure instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import hl7bridge
from hl7bridge import cli
from hl7bridge.errors import EXIT_CONNECTION, EXIT_OK, EXIT_USAGE, TlsConfigError
from hl7bridge.ingest.mllp import (
    TlsHandshakeError,
    client_ssl_context,
    handshake_failures,
    send_messages,
    serve,
    server_ssl_context,
)
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


@pytest.fixture(autouse=True)
def _need_openssl() -> None:
    if OPENSSL is None:
        message = "the openssl command line tool is not installed; cannot make test certs"
        if REQUIRED:
            pytest.fail(f"HL7BRIDGE_TLS_REQUIRED=1 but {message}")
        pytest.skip(message)


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

    def __init__(self, ctx: ssl.SSLContext | None, drop_at: int | None = None) -> None:
        self.ctx = ctx
        self.drop_at = drop_at
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
                raise ConnectionResetError  # the connection ends with no ACK
            return ACK

        async def junk(_data: bytes) -> None:
            return None

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
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def _send(port: int, ctx: ssl.SSLContext | None, host: str = "127.0.0.1") -> list[bytes]:
    return asyncio.run(send_messages(host, port, [MSG], ack_timeout_s=3.0, ssl_context=ctx))


REFUSED = (TlsHandshakeError, ssl.SSLError, ConnectionError)


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
    with TlsServer(server_ctx) as srv, pytest.raises(REFUSED):
        _send(srv.port, client_ssl_context(certs["ca"]))
    assert srv.received == []


def test_mutual_tls_refuses_a_client_certificate_from_another_ca(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    client_ctx = client_ssl_context(certs["ca"], certs["stranger_cert"], certs["stranger_key"])
    with TlsServer(server_ctx) as srv, pytest.raises(REFUSED):
        _send(srv.port, client_ctx)
    assert srv.received == []


def test_plain_client_is_refused_by_a_tls_server(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx) as srv, pytest.raises(REFUSED):
        _send(srv.port, None)
    assert srv.received == []


def test_tls_client_is_refused_by_a_plain_server(certs):
    with TlsServer(None) as srv, pytest.raises(REFUSED):
        _send(srv.port, client_ssl_context(certs["ca"]))
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


def test_cli_send_to_a_plain_listener_fails_with_a_tls_message(certs, capsys):
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


def test_a_stalled_handshake_says_nothing_was_sent(certs, capsys):
    # A listener that accepts the connection and then says nothing.
    with TlsServer(None) as srv:
        rc = cli.main(_send_args(srv.port, "--tls-ca", certs["ca"], "--timeout", "1"))
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert "no TLS handshake" in err and "Nothing was sent" in err


def test_system_store_context_is_built_like_the_ca_one():
    ctx = client_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.options & ssl.OP_NO_RENEGOTIATION
    assert ctx.keylog_filename is None or ctx.keylog_filename == os.environ.get("SSLKEYLOGFILE")


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
    with TlsServer(server_ctx) as srv, pytest.raises(REFUSED):
        _send(srv.port, client_ctx)
    assert srv.received == []


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_tls_1_1_is_refused(certs):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    old = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    old.check_hostname = False
    old.verify_mode = ssl.CERT_NONE
    try:
        old.set_ciphers("DEFAULT:@SECLEVEL=0")
        old.minimum_version = ssl.TLSVersion.TLSv1
        old.maximum_version = ssl.TLSVersion.TLSv1_1
    except (ssl.SSLError, ValueError):
        pytest.skip("this OpenSSL cannot offer TLS 1.1 at all")
    with TlsServer(server_ctx) as srv, pytest.raises(REFUSED):
        _send(srv.port, old)
    assert srv.received == []


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
    assert "tls handshake failed" in text and "peer=('127.0.0.1'" in text and "reason=" in text
    assert "MSH" not in text


def test_the_accepted_client_certificate_subject_is_logged(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    client_ctx = client_ssl_context(certs["ca"], certs["client_cert"], certs["client_key"])
    with (
        caplog.at_level(logging.INFO, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx) as srv,
    ):
        assert _send(srv.port, client_ctx) == [ACK]
    assert "client certificate" in caplog.text and "commonName=client" in caplog.text


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
    assert proc.stdout is not None
    line = proc.stdout.readline()  # printed once the socket is bound
    assert "MLLP over TLS listener on" in line, line
    port = int(line.split(" listener on ")[1].split(" ")[0].rsplit(":", 1)[1].rstrip(","))
    return proc, port


def test_listen_over_tls_end_to_end_and_replay_over_tls(certs, tmp_path, capsys):
    db = tmp_path / "store.db"
    proc, port = _spawn_listener(certs, db, "0.0.0.0")
    try:
        rc = cli.main(_send_args(port, "--tls-ca", certs["ca"]))
        out = capsys.readouterr()
        assert rc == EXIT_OK, out.err
        assert "got 1 ACK(s)" in out.out
        rows = sqlite3.connect(db).execute("select count(*) from messages").fetchone()[0]
        assert rows == 1
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=10)
    # No --tls-ca on a non-loopback bind: encrypted, but senders are not authenticated.
    assert "does not authenticate senders" in err

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
