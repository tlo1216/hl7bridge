"""MLLP over TLS: handshake, mutual TLS, and the refusals.

Certificates are made at test time with a throwaway CA by the ``openssl``
command line tool (the ``cryptography`` package is not a dependency). The
tests skip, with the reason, when the tool is not installed.
"""

from __future__ import annotations

import asyncio
import shutil
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from hl7bridge import cli
from hl7bridge.errors import EXIT_CONNECTION, EXIT_OK, EXIT_USAGE, TlsConfigError
from hl7bridge.ingest.mllp import client_ssl_context, send_messages, serve, server_ssl_context
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
pytestmark = pytest.mark.skipif(
    OPENSSL is None, reason="the openssl command line tool is not installed; cannot make test certs"
)

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
    }


class TlsServer:
    """An MLLP listener on a thread that answers every frame with AA."""

    def __init__(self, ctx: ssl.SSLContext | None) -> None:
        self.ctx = ctx
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.port = 0
        self.received: list[bytes] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def handle(payload: bytes) -> bytes:
            self.received.append(payload)
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


REFUSED = (ssl.SSLError, ConnectionError, TimeoutError)


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
    with TlsServer(server_ctx) as srv, pytest.raises(ssl.SSLCertVerificationError):
        _send(srv.port, client_ssl_context(certs["other_ca"]))
    assert srv.received == []


def test_expired_certificate_is_refused(certs):
    server_ctx = server_ssl_context(certs["expired_cert"], certs["expired_key"])
    with TlsServer(server_ctx) as srv, pytest.raises(ssl.SSLCertVerificationError, match="expired"):
        _send(srv.port, client_ssl_context(certs["ca"]))
    assert srv.received == []


def test_host_name_mismatch_is_refused(certs):
    server_ctx = server_ssl_context(certs["wrongname_cert"], certs["wrongname_key"])
    with (
        TlsServer(server_ctx) as srv,
        pytest.raises(ssl.SSLCertVerificationError, match=r"127.0.0.1"),
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
    assert "does not use TLS" in err or "TLS handshake" in err
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
