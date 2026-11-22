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
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest

import hl7bridge
from hl7bridge import cli
from hl7bridge.errors import (
    EXIT_CONNECTION,
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_USAGE,
    AckProtocolError,
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
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR

from .test_cli import _AckingListener, _Listener

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


def _ack_for(payload: bytes, code: str = "AA") -> bytes:
    """The ACK a correct listener sends: MSA-2 is the MSH-10 it was sent."""
    control_id = payload.split(b"\r")[0].split(b"|")[9]
    return (
        b"MSH|^~\\&|R|F|S|F|20261007||ACK|1|T|2.5\rMSA|" + code.encode() + b"|" + control_id + b"\r"
    )


ACK = _ack_for(MSG)
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

    def intermediate(self, parent: str, name: str) -> Path:
        """An issuing CA signed by ``parent``; ``leaf(name, ...)`` signs with it."""
        d = self.root / name
        d.mkdir()
        (d / "openssl.cnf").write_text(CA_CNF)
        (d / "index.txt").write_text("")
        (d / "serial").write_text("01\n")
        _run(
            d,
            "req",
            "-new",
            *EC,
            "-keyout",
            "ca.key",
            "-out",
            "ca.csr",
            "-subj",
            f"/CN=test {name}",
            "-config",
            "openssl.cnf",
        )
        ext = d / "ca.ext"
        ext.write_text("basicConstraints=critical,CA:TRUE\nkeyUsage=keyCertSign,cRLSign\n")
        parent_dir = self._dirs[parent]
        _run(
            parent_dir,
            "ca",
            "-batch",
            "-config",
            "openssl.cnf",
            "-in",
            str(d / "ca.csr"),
            "-out",
            str(d / "ca.pem"),
            "-notext",
            "-extfile",
            str(ext),
            "-startdate",
            "20200101000000Z",
            "-enddate",
            "20991231000000Z",
            "-keyfile",
            "ca.key",
            "-cert",
            "ca.pem",
        )
        self._dirs[name] = d
        return d / "ca.pem"

    def leaf(
        self,
        ca: str,
        name: str,
        san: str | None,
        *,
        usage: str = "serverAuth",
        start: str = "20200101000000Z",
        end: str = "20991231000000Z",
    ) -> tuple[str, str]:
        d = self._dirs[ca]
        ext = d / f"{name}.ext"
        lines = [f"extendedKeyUsage={usage}", "basicConstraints=CA:FALSE"]
        if san is not None:
            lines.insert(0, f"subjectAltName={san}")
        ext.write_text("\n".join(lines) + "\n")
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
    dns_only = pki.leaf("good", "dnsonly", "DNS:localhost")
    ip_only = pki.leaf("good", "iponly", "IP:127.0.0.1")
    cn_only = pki.leaf("good", "localhost", None)  # a CN of localhost and no SAN at all
    other_server = pki.leaf("other", "otherserver", "IP:127.0.0.1,DNS:localhost")
    issuing = pki.intermediate("good", "issuing")
    issued = pki.leaf("issuing", "issued", "IP:127.0.0.1,DNS:localhost")
    issued_client = pki.leaf("issuing", "issuedclient", "DNS:client.test", usage="clientAuth")
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
        "dns_only_cert": dns_only[0],
        "dns_only_key": dns_only[1],
        "ip_only_cert": ip_only[0],
        "ip_only_key": ip_only[1],
        "cn_only_cert": cn_only[0],
        "cn_only_key": cn_only[1],
        "other_server_cert": other_server[0],
        "other_server_key": other_server[1],
        "issuing_ca": str(issuing),
        "issued_cert": issued[0],
        "issued_key": issued[1],
        "issued_client_cert": issued_client[0],
        "issued_client_key": issued_client[1],
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
        reply: Callable[[bytes], bytes] | None = None,
        **serve_kw: object,
    ) -> None:
        self.reply = reply
        self.serve_kw = serve_kw
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
            return self.reply(payload) if self.reply else _ack_for(payload)

        async def junk(data: bytes) -> None:
            self.junk.append(data)

        async def start() -> None:
            self.server = await serve(
                "127.0.0.1", 0, handle, junk, ssl_context=self.ctx, **self.serve_kw
            )
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
    with pytest.raises(TlsConfigError, match="given with --tls-cert") as missing:
        server_ssl_context(str(tmp_path / "none.pem"), certs["server_key"])
    assert "none.pem" not in str(missing.value)
    # A certificate paired with the wrong key.
    with pytest.raises(TlsConfigError) as exc:
        server_ssl_context(certs["server_cert"], certs["client_key"])
    text = str(exc.value)
    assert "BEGIN" not in text and "PRIVATE" not in text
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    with pytest.raises(TlsConfigError, match="no usable PEM") as bad_ca:
        client_ssl_context(str(junk))
    assert "junk.pem" not in str(bad_ca.value)
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

    async def old_handshake(port: int) -> None:
        # Not send_messages: it refuses a context that does not verify.
        _r, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=old, ssl_handshake_timeout=3
        )
        writer.close()

    with caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"), TlsServer(server_ctx) as srv:
        with pytest.raises(REFUSED):
            asyncio.run(old_handshake(srv.port))
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
        caplog.at_level(logging.DEBUG, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx) as srv,
    ):
        assert _send(srv.port, client_ctx) == [ACK]
    records = [r for r in caplog.records if "certificate peer=" in r.getMessage()]
    assert len(records) == 1
    # NOTICE sits above WARNING, so --quiet does not hide it.
    assert records[0].levelno == NOTICE > logging.WARNING
    text = records[0].getMessage()
    assert "sha256=" in text and "serial=" in text and "issuer=" not in text
    details = [r.getMessage() for r in caplog.records if "certificate details" in r.getMessage()]
    assert len(details) == 1 and details[0].startswith("mllp client certificate details")
    assert "commonName%3Dclient" in details[0] and "DNS%3Aclient.test" in details[0]
    assert "test%20good" in details[0]  # the issuer


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


def _spawn_listener(
    certs, db: Path, host: str, *extra: str, preamble: str = ""
) -> tuple[subprocess.Popen, int]:
    src = str(Path(hl7bridge.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src}
    # A background job can start with SIGINT ignored; Ctrl+C must work in the child.
    code = preamble + (
        "import signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler);"
        " from hl7bridge.cli import main; sys.exit(main())"
    )
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
        stdout = proc.stdout
        assert stdout is not None
        # Printed once the socket is bound. A child that never prints must not
        # hang the test: the wait is bounded and the child is then killed.
        lines: list[str] = []
        waiter = threading.Thread(target=lambda: lines.append(stdout.readline()), daemon=True)
        waiter.start()
        waiter.join(30)
        assert lines, "the listener printed no banner within 30 s"
        line = lines[0]
        assert "MLLP over TLS" in line and " listener on " in line, line
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

    monkeypatch.setattr(mllp, "_open_connection", fail)
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


class _FakeSsl:
    def __init__(self, der: bytes) -> None:
        self.der = der

    def getpeercert(self, binary_form: bool = False):
        return self.der if binary_form else None


class _FakeWriter:
    transport = None

    def __init__(self, cert, der: bytes = b"der-bytes") -> None:
        self.cert = cert
        self.der = der

    async def start_tls(self, *_a, **_k) -> None:
        return None

    def get_extra_info(self, name):
        return {
            "peername": ("10.1.1.1", 4000),
            "peercert": self.cert,
            "ssl_object": _FakeSsl(self.der),
        }.get(name)


def test_the_client_certificate_line_is_structured_and_carries_a_fingerprint(caplog):
    import hashlib

    forged = {
        "subject": ((("commonName", 'evil"\nx serial=0x1 \x1b]0;pwn\x07'),),),
        "issuer": ((("commonName", "ca\r\nWARNING forged"),),),
        "serialNumber": "0A\x00B",
        "subjectAltName": (("DNS", "a.test\nb"), ("IP Address", "10.0.0.1")),
    }
    san_only = {
        "subject": (),
        "issuer": ((("commonName", "ca"),),),
        "serialNumber": "01",
        "subjectAltName": (("DNS", "only.test"),),
    }
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with caplog.at_level(logging.DEBUG, logger="hl7bridge.ingest.mllp"):
        assert asyncio.run(mllp._start_tls(_FakeWriter(forged), ctx))  # type: ignore[arg-type]
        assert asyncio.run(mllp._start_tls(_FakeWriter(san_only, b"other"), ctx))  # type: ignore[arg-type]
        # A client without a certificate (no --tls-ca) logs no identity line.
        assert asyncio.run(mllp._start_tls(_FakeWriter(None), ctx))  # type: ignore[arg-type]
    notices = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == NOTICE and "client certificate" in r.getMessage()
    ]
    details = [r.getMessage() for r in caplog.records if "certificate details" in r.getMessage()]
    assert len(notices) == 2 and len(details) == 2
    fingerprint = hashlib.sha256(b"der-bytes").hexdigest()
    assert f"sha256={fingerprint}" in notices[0] and "peer=10.1.1.1:4000" in notices[0]
    assert f"sha256={hashlib.sha256(b'other').hexdigest()}" in notices[1]
    for line in (*notices, *details):
        assert "\n" not in line and "\x1b" not in line and "\x00" not in line
        # Every token is name=value with one equals sign and no space inside a
        # value, so a whitespace-based collector cannot read a forged field.
        tokens = [
            t for t in line.split(" ") if t not in ("mllp", "client", "certificate", "details")
        ]
        assert all(t.count("=") == 1 for t in tokens), tokens
    assert " serial=0x1" not in details[0] and details[0].count(" serial=") == 0
    assert notices[0].count(" serial=") == 1
    # The always-shown line holds no subject or alternative name.
    assert "subject=" not in notices[0] and "san=" not in notices[0]
    assert "only.test" in details[1].replace("%3A", ":")


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

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
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


# -- Findings from the third Nov 21 review -----


def _frame_of(buffer: bytes) -> bytes:
    return buffer[1:-2]


@contextlib.contextmanager
def _scripted_listener(*replies):
    """The shared test responder with one reply per frame (see _AckingListener)."""
    with _AckingListener(script=replies) as listener:
        yield listener.port, listener.seen


def _good(payload: bytes) -> bytes:
    return mllp.encode_mllp(_ack_for(payload))


def _one_reply(reply: bytes, payload: bytes = MSG, **kw) -> tuple[list[bytes], object]:
    """send_messages of one payload to the shared responder, which answers
    ``reply``. Returns (acks kept, the error or None)."""
    acks: list[bytes] = []
    with _scripted_listener(mllp.encode_mllp(reply)) as (port, _seen):
        try:
            asyncio.run(send_messages("127.0.0.1", port, [payload], acks=acks, **kw))
        except Exception as exc:  # noqa: BLE001  the test inspects it
            return acks, exc
    return acks, None


async def _raw_send(script, payloads, **kw):
    """send_messages against a plain listener written as ``script(reader, writer)``.
    Returns (acks kept, the error or None)."""

    async def handle(reader, writer):
        try:
            await script(reader, writer)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    acks: list[bytes] = []
    error: BaseException | None = None
    try:
        await send_messages("127.0.0.1", port, payloads, acks=acks, **kw)
    except BaseException as exc:  # noqa: BLE001  the test inspects it
        error = exc
    finally:
        server.close()
        await server.wait_closed()
    return acks, error


async def _read_frame(reader) -> bytes:
    return _frame_of(await reader.readuntil(b"\x1c\r"))


# High 1 and the empty host (send, replay, listen)


@pytest.mark.parametrize("host", ["", "  "])
@pytest.mark.parametrize("command", ["send", "replay", "listen"])
def test_an_empty_host_is_refused_before_any_network_call(
    command, host, tmp_path, capsys, monkeypatch
):
    async def never(*_a, **_k):
        raise AssertionError("the network was touched")

    monkeypatch.setattr(mllp, "_open_connection", never)
    monkeypatch.setattr(mllp.asyncio, "start_server", never)
    db = tmp_path / "never.db"
    argv = {
        "send": ["send", str(SAMPLES_DIR / "01_hospa_adt_a04.hl7")],
        "replay": ["replay", "--from", "store", "--db", str(db)],
        "listen": ["listen", "--port", "0", "--db", str(db)],
    }[command]
    rc = cli.main([*argv, "--host", host, *(["--tls"] if command != "listen" else [])])
    assert rc == EXIT_USAGE
    assert "--host is empty" in capsys.readouterr().err
    assert not db.exists()


def test_send_messages_and_serve_refuse_an_empty_host(certs, monkeypatch):
    async def never(*_a, **_k):
        raise AssertionError("the network was touched")

    monkeypatch.setattr(mllp, "_open_connection", never)
    monkeypatch.setattr(mllp.asyncio, "start_server", never)
    for host in ("", "   "):
        with pytest.raises(ValueError, match="host"):
            asyncio.run(send_messages(host, 1, [MSG], ssl_context=client_ssl_context(certs["ca"])))
        with pytest.raises(ValueError, match="host"):
            asyncio.run(serve(host, 0, lambda p: p, lambda d: d))  # type: ignore[arg-type,return-value]


# Medium 2: a key value is never echoed


# A synthetic value shaped like key text, not a key.
PEM = (
    "-----BEGIN PRIVATE KEY-----\nMIIsecretsecret\n-----END PRIVATE KEY-----\n"  # guardrails: allow
)


@pytest.mark.parametrize("value", [PEM, "bad\x00name", "a\nb"], ids=["pem", "nul", "newline"])
@pytest.mark.parametrize("flag", ["--tls-key", "--tls-cert", "--tls-ca"])
@pytest.mark.parametrize("command", ["send", "listen"])
def test_a_tls_argument_value_is_never_echoed(command, flag, value, certs, tmp_path, capsys):
    paired = {"--tls-key": ["--tls-cert", certs["server_cert"]], "--tls-cert": [], "--tls-ca": []}
    if flag == "--tls-cert":
        paired[flag] = ["--tls-key", certs["server_key"]]
    if flag == "--tls-ca" and command == "listen":
        paired[flag] = ["--tls-cert", certs["server_cert"], "--tls-key", certs["server_key"]]
    argv = (
        _send_args(1, *paired[flag], flag, value)
        if command == "send"
        else ["listen", "--port", "0", "--db", str(tmp_path / "n.db"), *paired[flag], flag, value]
    )
    rc = cli.main(argv)
    err = capsys.readouterr().err
    assert rc == EXIT_USAGE
    assert flag in err
    assert "BEGIN" not in err and "secret" not in err and "bad" not in err and "a\nb" not in err
    assert err.count("\n") == 1, "one error line, however many line breaks the value had"


# Medium 3: the ACK must answer the message


def test_the_happy_path_ack_answers_the_message_it_was_sent():
    assert b"MSA|AA|SMPA0001" in ACK  # the sample's own MSH-10, not a made-up number


def test_a_stale_aa_for_another_message_is_a_protocol_failure():
    stale = mllp.encode_mllp(b"MSH|^~\\&|R|F|S|F|20261007||ACK|1|T|2.5\rMSA|AA|1\r")

    async def script(reader, writer):
        await _read_frame(reader)
        writer.write(stale)
        await writer.drain()

    acks, error = asyncio.run(_raw_send(script, [MSG]))
    assert isinstance(error, AckProtocolError)
    assert acks == []
    assert "SMPA0001" not in str(error)  # fixed text only


def test_cli_send_stops_on_a_wrong_ack_and_keeps_the_confirmed_ones(tmp_path, capsys):
    stale = mllp.encode_mllp(b"MSH|^~\\&|R|F|S|F|20261007||ACK|1|T|2.5\rMSA|AA|NOT-OURS\r")
    with _scripted_listener(_good, stale) as (port, _seen):
        rc = cli.main(["send", str(_many(tmp_path, 3)), "--port", str(port)])
    captured = capsys.readouterr()
    assert rc == EXIT_CONNECTION
    assert "not a valid ACK" in captured.err and "after 1 of 3 ACKs" in captured.err
    assert "NOT-OURS" not in captured.err + captured.out
    assert "sent 2 message(s)" in captured.out and "got 1 ACK(s)" in captured.out
    assert captured.out.count("not sent") == 1
    assert "internal error" not in captured.err


def test_the_bridges_own_acks_for_odd_control_ids_are_accepted():
    # Escaped separators in MSH-10 come back escaped; a bare subcomponent
    # separator splits it, and the listener reads (and echoes) the first part,
    # as the sender does. (An empty MSH-10 is never sent.)
    for sent, back in (
        (b"A\\F\\B", b"A\\F\\B"),
        (b"A&B", b"A"),
        (b"A\\T\\B", b"A\\T\\B"),
        (b"A1", b"A1"),
    ):
        payload = b"MSH|^~\\&|S|F|R|F|20261007||ADT^A04|" + sent + b"|P|2.5\rPID|1\r"
        ack = b"MSH|^~\\&|R|F|S|F|20261007||ACK|1|P|2.5\rMSA|AA|" + back + b"\r"

        async def script(reader, writer, ack=ack):
            await _read_frame(reader)
            writer.write(mllp.encode_mllp(ack))
            await writer.drain()

        acks, error = asyncio.run(_raw_send(script, [payload]))
        assert error is None and acks == [ack]


# Medium 4: no peer free text on stdout by default


def _text_ack(payload: bytes) -> bytes:
    control_id = payload.split(b"\r")[0].split(b"|")[9]
    return mllp.encode_mllp(
        b"MSH|^~\\&|R|F|S|F|20261007||ACK|1|P|2.5\rMSA|AE|"
        + control_id
        + b"|rejected MRN 12345678 Doe\rERR|||MRN 12345678 unknown\r"
    )


def test_a_negative_ack_prints_no_free_text_unless_asked(tmp_path, capsys):
    msg = tmp_path / "a.hl7"
    msg.write_bytes(MSG)
    with _scripted_listener(_text_ack) as (port, _seen):
        rc = cli.main(["send", str(msg), "--port", str(port)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "AE (application error" in out
    assert "12345678" not in out and "MSA-3" not in out and "ERR:" not in out
    with _scripted_listener(_text_ack) as (port, _seen):
        rc = cli.main(["send", str(msg), "--port", str(port), "--show-ack-text"])
    out = capsys.readouterr().out
    assert "MSA-3: rejected MRN 12345678 Doe" in out
    assert "ERR: MRN 12345678 unknown" in out


# Medium 5: the handshake tables are bounded


def test_the_failed_handshake_peer_table_is_a_fixed_size_lru(monkeypatch, caplog):
    monkeypatch.setattr(mllp, "HANDSHAKE_PEER_TABLE_MAX", 3)
    err = ssl.SSLError("x")
    with caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"):
        mllp._note_handshake_failure(("10.0.0.1", 1), err)
        mllp._note_handshake_failure(("10.0.0.1", 2), err)  # suppressed in 10.0.0.1's window
        for n in range(2, 12):
            mllp._note_handshake_failure((f"10.0.0.{n}", 1), err)
    assert len(mllp._windows) == 3
    assert list(mllp._windows) == ["10.0.0.9", "10.0.0.10", "10.0.0.11"]
    # The suppressed repeat of the first peer was reported when it was forgotten.
    assert any(
        "1 more time(s)" in r.getMessage() and "10.0.0.1'" in r.getMessage() for r in caplog.records
    )
    assert handshake_failures() == 12


def _closed_count(socks: list[socket.socket]) -> int:
    """How many of these connections the listener has closed; all are then closed."""
    closed = 0
    for sock in socks:
        sock.settimeout(0.5)
        try:
            if sock.recv(1) == b"":
                closed += 1
        except OSError as exc:
            if not isinstance(exc, TimeoutError):
                closed += 1
    for sock in socks:
        sock.close()
    return closed


def test_pending_handshakes_are_capped_with_a_log_line(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    socks = []
    with (
        caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx, max_pending_handshakes=2) as srv,
    ):
        for _ in range(6):  # connect and say nothing: the handshake never starts
            socks.append(socket.create_connection(("127.0.0.1", srv.port), timeout=5))
        time.sleep(0.5)
        closed = _closed_count(socks)
    assert closed >= 4, closed  # all but the two allowed to wait
    text = caplog.text
    assert "too many pending TLS handshakes" in text and "limit 2" in text


# Medium 6: shutdown does not wait for idle clients


def _tls_handler_server(certs, handler=None, **kw):
    async def default(payload: bytes) -> bytes:
        return _ack_for(payload)

    async def junk(_data: bytes) -> None:
        return None

    ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    return serve("127.0.0.1", 0, handler or default, junk, ssl_context=ctx, **kw)


def test_shutdown_closes_an_idle_client_at_once(certs):
    async def run() -> float:
        server = await _tls_handler_server(certs)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=client_ssl_context(certs["ca"]), server_hostname="127.0.0.1"
        )
        await asyncio.sleep(0.3)  # the server has finished the handshake and waits for a frame
        start = time.monotonic()
        await mllp.shutdown_server(server, grace_s=30.0)
        elapsed = time.monotonic() - start
        with contextlib.suppress(ConnectionError, ssl.SSLError):
            assert await asyncio.wait_for(reader.read(10), 3) == b""
        writer.close()
        return elapsed

    assert asyncio.run(run()) < 3.0  # not the 30 s grace: nobody was busy


def test_shutdown_gives_a_busy_client_the_grace_and_then_cuts_it(certs):
    async def run() -> tuple[float, bool]:
        started = asyncio.Event()
        finished: list[str] = []

        async def slow(payload: bytes) -> bytes:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                finished.append("cancelled")
                raise
            return _ack_for(payload)

        server = await _tls_handler_server(certs, slow)
        port = server.sockets[0].getsockname()[1]
        _reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=client_ssl_context(certs["ca"]), server_hostname="127.0.0.1"
        )
        writer.write(mllp.encode_mllp(MSG))
        await writer.drain()
        await asyncio.wait_for(started.wait(), 5)
        start = time.monotonic()
        await mllp.shutdown_server(server, grace_s=0.4)
        elapsed = time.monotonic() - start
        writer.close()
        return elapsed, bool(finished)

    elapsed, cancelled = asyncio.run(run())
    assert 0.3 <= elapsed < 5.0
    assert cancelled


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot deliver SIGINT to a process")
def test_ctrl_c_stops_a_listener_that_still_has_an_idle_tls_client(certs, tmp_path):
    proc, port = _spawn_listener(certs, tmp_path / "i.db", "127.0.0.1")
    try:
        ctx = client_ssl_context(certs["ca"])
        with (
            socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
            ctx.wrap_socket(raw, server_hostname="127.0.0.1"),
        ):
            time.sleep(0.3)
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=20)
    except BaseException:
        proc.kill()
        proc.communicate(timeout=10)
        raise
    assert proc.returncode == 0, err
    assert "listener stopped" in out


# Medium 7: a truncated ACK is a protocol failure, and confirmed results stay


def test_a_truncated_ack_is_a_protocol_failure_not_a_value_error():
    async def script(reader, writer):
        await _read_frame(reader)
        writer.write(_good(MSG))
        await _read_frame(reader)
        writer.write(b"\x0bMSH|^~\\&|R|F|S|F|2026")  # cut short, then the connection ends
        await writer.drain()

    acks, error = asyncio.run(_raw_send(script, [MSG, MSG, MSG]))
    assert isinstance(error, AckProtocolError)
    assert acks == [_ack_for(MSG)]


def test_cli_keeps_the_first_ack_when_the_second_is_truncated(tmp_path, capsys):
    with _scripted_listener(_good, b"\x0bMSH|^~\\&|R|F|S|F|2026") as (port, _seen):
        rc = cli.main(["send", str(_many(tmp_path, 2)), "--port", str(port)])
    captured = capsys.readouterr()
    assert rc == EXIT_CONNECTION
    assert "internal error" not in captured.err
    assert "not a valid ACK" in captured.err and "after 1 of 2 ACKs" in captured.err
    assert "got 1 ACK(s)" in captured.out and "  AA: 1" in captured.out


# Medium 8: the public sender enforces verification


@pytest.mark.parametrize("weaken", ["no_hostname", "none"])
def test_send_messages_refuses_a_context_that_does_not_verify(weaken, certs, monkeypatch):
    ctx = client_ssl_context(certs["ca"])
    ctx.check_hostname = False
    if weaken == "none":
        ctx.verify_mode = ssl.CERT_NONE
    elif weaken == "no_hostname":
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    async def never(*_a, **_k):
        raise AssertionError("the network was touched")

    monkeypatch.setattr(mllp, "_open_connection", never)
    with pytest.raises(TlsConfigError, match="verif"):
        asyncio.run(send_messages("127.0.0.1", 1, [MSG], ssl_context=ctx))


def test_send_messages_refuses_a_context_that_does_not_require_a_certificate(certs, monkeypatch):
    ctx = client_ssl_context(certs["ca"])
    ctx.verify_mode = ssl.CERT_OPTIONAL

    async def never(*_a, **_k):
        raise AssertionError("the network was touched")

    monkeypatch.setattr(mllp, "_open_connection", never)
    with pytest.raises(TlsConfigError, match="CERT_REQUIRED"):
        asyncio.run(send_messages("127.0.0.1", 1, [MSG], ssl_context=ctx))


# Lower severity


def test_common_name_is_not_used_for_host_names(certs):
    assert client_ssl_context(certs["ca"]).hostname_checks_common_name is False
    assert client_ssl_context().hostname_checks_common_name is False
    server_ctx = server_ssl_context(certs["cn_only_cert"], certs["cn_only_key"])
    with TlsServer(server_ctx) as srv, pytest.raises(TlsHandshakeError, match="localhost"):
        _send(srv.port, client_ssl_context(certs["ca"]), host="localhost")
    assert srv.received == []


def test_partial_chain_is_on_only_with_an_explicit_ca(certs):
    flag = ssl.VERIFY_X509_PARTIAL_CHAIN
    assert client_ssl_context(certs["ca"]).verify_flags & flag
    assert not client_ssl_context().verify_flags & flag
    assert (
        server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"]).verify_flags
        & flag
    )
    assert not server_ssl_context(certs["server_cert"], certs["server_key"]).verify_flags & flag


def test_an_issuing_ca_alone_is_a_trust_anchor(certs):
    server_ctx = server_ssl_context(certs["issued_cert"], certs["issued_key"])
    with TlsServer(server_ctx) as srv:
        assert _send(srv.port, client_ssl_context(certs["issuing_ca"])) == [ACK]
    # The root of another hierarchy still does not vouch for it.
    with TlsServer(server_ctx) as srv, pytest.raises(TlsHandshakeError):
        _send(srv.port, client_ssl_context(certs["other_ca"]))
    # Mutual TLS with only the issuing CA given.
    mtls = server_ssl_context(certs["server_cert"], certs["server_key"], certs["issuing_ca"])
    client = client_ssl_context(
        certs["ca"], certs["issued_client_cert"], certs["issued_client_key"]
    )
    with TlsServer(mtls) as srv:
        assert _send(srv.port, client) == [ACK]


def test_the_sent_total_counts_the_frames_written(certs, tmp_path, capsys):
    folder = _many(tmp_path, 3)
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx, delay_s=3.0) as srv:
        cli.main(
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
    out = capsys.readouterr().out
    assert "sent 1 message(s)" in out and "got 0 ACK(s)" in out
    assert out.count(": not sent") == 2


def test_one_deadline_covers_the_connect(monkeypatch, capsys):
    async def blackhole(*_a, **_k):
        await asyncio.sleep(60)

    monkeypatch.setattr(mllp, "_open_connection", blackhole)
    start = time.monotonic()
    rc = cli.main(_send_args(1, "--timeout", "0.5"))
    assert time.monotonic() - start < 3.0
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert "timed out" in err and "Never connected" in err


def test_a_subsecond_timeout_is_not_stretched_by_the_handshake(certs, capsys):
    with TlsServer(None) as srv:  # a plain listener never answers a ClientHello
        start = time.monotonic()
        rc = cli.main(_send_args(srv.port, "--tls-ca", certs["ca"], "--timeout", "0.3"))
        elapsed = time.monotonic() - start
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert elapsed < 0.9, elapsed  # it used to wait a full second
    assert "no TLS handshake" in err and "within 0.3 s" in err


def test_the_deadline_covers_a_write_the_peer_never_drains():
    async def script(reader, writer):
        await asyncio.sleep(2)  # accepts and never reads

    big = b"MSH|^~\\&|S|F|R|F|20261007||ORU^R01|BIG1|P|2.5\r" + b"OBX|" + b"x" * 40_000_000 + b"\r"
    start = time.monotonic()
    acks, error = asyncio.run(_raw_send(script, [big], ack_timeout_s=0.7))
    assert isinstance(error, TimeoutError)
    assert time.monotonic() - start < 6.0
    assert acks == []


def _feed(*chunks: bytes) -> list[object]:
    async def run() -> list[object]:
        reader = asyncio.StreamReader()
        for chunk in chunks:
            reader.feed_data(chunk)
        reader.feed_eof()
        return [item async for item in decode_frames(reader)]

    return asyncio.run(run())


def test_the_frame_cap_is_exact_for_terminated_and_unterminated_frames():
    cap = mllp.MAX_FRAME_BYTES
    at_cap = _feed(b"\x0b" + b"a" * cap + b"\x1c\r")
    assert at_cap == [Frame(b"a" * cap)]
    over = _feed(b"\x0b" + b"a" * (cap + 1) + b"\x1c\r")
    assert [type(i) for i in over] == [JunkBytes]  # same read as its terminator
    # Unterminated: exactly the cap waits for more bytes, one more is quarantined.
    waiting = _feed(b"\x0b" + b"a" * cap)
    assert [type(i) for i in waiting] == [JunkBytes]  # at EOF it is junk, never a frame
    spill = _feed(b"\x0b" + b"a" * (cap + 1))
    assert sum(len(i.data) for i in spill if isinstance(i, JunkBytes)) == cap + 2


def test_an_oversized_frame_never_reaches_the_handler(certs):
    seen: list[bytes] = []

    async def handler(payload: bytes) -> bytes:
        seen.append(payload)
        return _ack_for(payload)

    async def run() -> list[bytes]:
        junk: list[bytes] = []

        async def on_junk(data: bytes) -> None:
            junk.append(data)

        server = await serve("127.0.0.1", 0, handler, on_junk)
        port = server.sockets[0].getsockname()[1]
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x0b" + b"a" * (mllp.MAX_FRAME_BYTES + 5) + b"\x1c\r")
        await writer.drain()
        await asyncio.sleep(0.5)
        writer.close()
        await mllp.shutdown_server(server, grace_s=1.0)
        return junk

    junk = asyncio.run(run())
    assert seen == [] and junk


def test_replay_checks_tls_before_it_looks_at_the_store(certs, tmp_path, capsys):
    from hl7bridge.store import connect

    db = tmp_path / "r.db"
    connect(db).close()  # an empty store: nothing to replay
    missing = str(tmp_path / "no-ca.pem")
    for extra in ([], ["--dry-run"]):
        rc = cli.main(["replay", "--from", "store", "--db", str(db), "--tls-ca", missing, *extra])
        err = capsys.readouterr().err
        assert rc == EXIT_USAGE and "--tls-ca" in err, extra
        assert "nothing to replay" not in err


def test_the_docs_state_the_windows_limits():
    root = Path(hl7bridge.__file__).resolve().parents[2]
    cli_doc = (root / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "Disallowed" in cli_doc
    assert "TerminateProcess" in cli_doc or "terminate" in cli_doc.lower()


# Test gaps: CLI mTLS wiring, the actual host name, trust-store selection


def test_the_cli_listener_really_requires_a_client_certificate(certs, tmp_path):
    proc, port = _spawn_listener(certs, tmp_path / "m.db", "127.0.0.1", "--tls-ca", certs["ca"])
    try:
        _refused(port, client_ssl_context(certs["ca"]))  # no client certificate
        stranger = client_ssl_context(certs["ca"], certs["stranger_cert"], certs["stranger_key"])
        _refused(port, stranger)  # a certificate from another CA
        good = client_ssl_context(certs["ca"], certs["client_cert"], certs["client_key"])
        assert len(_send(port, good)) == 1  # the real listener's own ACK
    finally:
        proc.kill()
        proc.communicate(timeout=10)


@pytest.mark.parametrize(
    ("cert", "host", "ok"),
    [
        ("dns_only", "localhost", True),
        ("dns_only", "127.0.0.1", False),
        ("ip_only", "127.0.0.1", True),
        ("ip_only", "localhost", False),
    ],
)
def test_the_name_checked_is_the_name_given(cert, host, ok, certs):
    server_ctx = server_ssl_context(certs[f"{cert}_cert"], certs[f"{cert}_key"])
    with TlsServer(server_ctx) as srv:
        if ok:
            assert _send(srv.port, client_ssl_context(certs["ca"]), host=host) == [ACK]
        else:
            with pytest.raises(TlsHandshakeError) as exc:
                _send(srv.port, client_ssl_context(certs["ca"]), host=host)
            assert isinstance(exc.value.cause, ssl.SSLCertVerificationError)
    assert bool(srv.received) is ok


def _default_roots(monkeypatch, cafile: str) -> None:
    def load(self, purpose=ssl.Purpose.SERVER_AUTH):
        self.load_verify_locations(cafile=cafile)

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", load)


def test_the_default_trust_store_is_used_when_no_ca_is_given(certs, monkeypatch):
    _default_roots(monkeypatch, certs["ca"])
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with TlsServer(server_ctx) as srv:
        assert _send(srv.port, client_ssl_context()) == [ACK]
    _default_roots(monkeypatch, certs["other_ca"])
    with TlsServer(server_ctx) as srv, pytest.raises(TlsHandshakeError):
        _send(srv.port, client_ssl_context())


def test_an_explicit_ca_replaces_the_default_store_in_both_directions(certs, monkeypatch):
    # The system store trusts the other CA; the explicit CA is the good one.
    _default_roots(monkeypatch, certs["other_ca"])
    good = server_ssl_context(certs["server_cert"], certs["server_key"])
    other = server_ssl_context(certs["other_server_cert"], certs["other_server_key"])
    with TlsServer(good) as srv:
        assert _send(srv.port, client_ssl_context(certs["ca"])) == [ACK]  # the explicit CA works
    with TlsServer(other) as srv, pytest.raises(TlsHandshakeError):
        _send(srv.port, client_ssl_context(certs["ca"]))  # and the default is not added to it
    # And the other way round: the default trusts the good CA, the explicit one does not.
    _default_roots(monkeypatch, certs["ca"])
    with TlsServer(good) as srv, pytest.raises(TlsHandshakeError):
        _send(srv.port, client_ssl_context(certs["other_ca"]))


# -- Findings from the final Nov 21 review (R1 to R11) ---------------------------------


def _with_control_id(control_id: bytes, payload: bytes = MSG) -> bytes:
    first, rest = payload.split(b"\r", 1)
    fields = first.split(b"|")
    fields[9] = control_id
    return b"|".join(fields) + b"\r" + rest


def _store_rows(db: Path) -> tuple[int, int]:
    """(messages, quarantine) rows in a store."""
    conn = sqlite3.connect(db)
    try:
        messages = conn.execute("select count(*) from messages").fetchone()[0]
        quarantine = conn.execute("select count(*) from quarantine").fetchone()[0]
    finally:
        conn.close()
    return messages, quarantine


# R1: the ACK must answer the message, judged the same way on both sides


def test_the_real_listeners_ar_for_an_unparseable_message_is_reported(tmp_path, capsys):
    msg = tmp_path / "bad.hl7"
    msg.write_bytes(MSG.replace(b"PID|", b"PID|\xff", 1))  # not UTF-8
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(msg), "--port", str(listener.port)])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS, captured.err
    assert ": AR (" in captured.out and "quarantine reason: parse_error" in captured.out
    assert "not a valid ACK" not in captured.err
    assert _store_rows(tmp_path / "s.db") == (0, 1)


def test_the_real_listeners_ar_for_an_internal_error_is_reported(tmp_path, capsys, monkeypatch):
    from hl7bridge.ingest import IngestPipeline

    def boom(self, raw):
        raise RuntimeError("injected")

    monkeypatch.setattr(IngestPipeline, "handle_bytes", boom)
    msg = tmp_path / "a.hl7"
    msg.write_bytes(MSG)
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(msg), "--port", str(listener.port)])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS, captured.err
    assert ": AR (" in captured.out and "quarantine reason: internal_error" in captured.out


def test_the_listener_echoes_a_readable_msh10_in_parse_and_internal_error_acks(tmp_path):
    from hl7bridge.config import HospitalRegistry
    from hl7bridge.ingest import IngestPipeline
    from hl7bridge.store import connect

    conn = connect(tmp_path / "p.db")
    try:
        pipeline = IngestPipeline(HospitalRegistry.load_dir(BUNDLED_PROFILES), conn)
        ack = pipeline.handle_bytes(MSG.replace(b"PID|", b"PID|\xff", 1))
        assert "\rMSA|AR|SMPA0001|parse_error" in ack
        ack = pipeline._internal_error_ack(MSG, "RuntimeError", "").decode()
        assert "\rMSA|AR|SMPA0001|internal_error" in ack
        # No readable MSH: the placeholder stays.
        ack = pipeline.handle_bytes(b"not hl7 at all")
        assert "\rMSA|AR|UNKNOWN|parse_error" in ack
    finally:
        conn.close()


def test_escaped_and_padded_control_ids_match_through_the_real_listener(tmp_path, capsys):
    folder = tmp_path / "ids"
    folder.mkdir()
    ids = [b" PAD01 ", b"ESC\\F\\02", b"HEX\\X41\\03", b"CMP04^x", b"SUB\\T\\05"]
    for n, control_id in enumerate(ids):
        (folder / f"m{n}.hl7").write_bytes(_with_control_id(control_id))
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(folder), "--port", str(listener.port)])
    captured = capsys.readouterr()
    assert rc == EXIT_OK, captured.err
    assert "got 5 ACK(s)" in captured.out and "  AA: 5" in captured.out
    assert _store_rows(tmp_path / "s.db") == (5, 0)


@pytest.mark.parametrize("control_id", [b"", b"   ", b"^x"], ids=["empty", "blank", "component"])
def test_an_empty_control_id_is_refused_before_anything_is_sent(control_id, tmp_path, capsys):
    msg = tmp_path / "a.hl7"
    msg.write_bytes(_with_control_id(control_id))
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(msg), "--port", str(listener.port)])
    captured = capsys.readouterr()
    assert rc == EXIT_USAGE
    assert "MSH-10" in captured.err and "nothing sent" in captured.err
    assert _store_rows(tmp_path / "s.db") == (0, 0)


@pytest.mark.parametrize(
    ("msa", "ok"),
    [(b"AA|UNKNOWN", False), (b"AA|", False), (b"CA|UNKNOWN", False), (b"AR|UNKNOWN", True)],
)
def test_send_messages_never_confirms_a_message_without_a_control_id(msa, ok):
    # The library sends it only when asked (replay does); send refuses it.
    ack = _HEADER + b"MSA|" + msa + b"\r"
    acks, error = _one_reply(ack, _with_control_id(b""), allow_missing_control_id=True)
    if ok:
        assert error is None and acks == [ack]
    else:
        assert isinstance(error, AckProtocolError) and acks == []


_HEADER = b"MSH|^~\\&|R|F|S|F|20261007||ACK|1|P|2.5\r"


@pytest.mark.parametrize(
    "reply",
    [
        b"MSH|^~\\&|R|F|S|F|20261007||ADT^A04|1|P|2.5\rMSA|AA|SMPA0001\r",
        b"MSA|AA|SMPA0001\r",
        _HEADER + b"MSA|AA|SMPA0001\rMSA|AE|SMPA0001\r",
        _HEADER,
    ],
    ids=["not_an_ack", "no_msh", "two_msa", "no_msa"],
)
def test_a_reply_that_is_not_one_ack_is_never_success(reply):
    acks, error = _one_reply(reply)
    assert isinstance(error, AckProtocolError), error
    assert acks == []


def test_cli_send_does_not_count_an_aa_in_a_non_ack_reply(tmp_path, capsys):
    reply = mllp.encode_mllp(b"MSH|^~\\&|R|F|S|F|20261007||ORU^R01|1|P|2.5\rMSA|AA|SMPA0001\r")
    msg = tmp_path / "a.hl7"
    msg.write_bytes(MSG)
    with _scripted_listener(reply) as (port, _seen):
        rc = cli.main(["send", str(msg), "--port", str(port)])
    captured = capsys.readouterr()
    assert rc == EXIT_CONNECTION
    assert "not a valid ACK" in captured.err and "got 0 ACK(s)" in captured.out


@pytest.mark.parametrize(
    ("code", "ok"),
    [("AA", False), ("CA", False), ("AE", True), ("AR", True), ("CE", True), ("CR", True)],
)
def test_unknown_answers_a_message_only_in_a_negative_ack(code, ok):
    ack = _HEADER + b"MSA|" + code.encode() + b"|UNKNOWN|parse_error: MSH\r"
    acks, error = _one_reply(ack)
    if ok:
        assert error is None and acks == [ack]
    else:
        assert isinstance(error, AckProtocolError) and acks == []


# R2: open connections and one address's handshakes are bounded


def test_open_tls_connections_are_capped_with_a_log_line(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    client_ctx = client_ssl_context(certs["ca"])
    with (
        caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx, max_connections=2) as srv,
    ):
        held = []
        for _ in range(2):
            raw = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
            held.append(client_ctx.wrap_socket(raw, server_hostname="127.0.0.1"))
        time.sleep(0.3)
        extra = [socket.create_connection(("127.0.0.1", srv.port), timeout=5) for _ in range(3)]
        time.sleep(0.5)
        closed = _closed_count(extra)
        held[0].sendall(mllp.encode_mllp(MSG))  # the admitted ones still work
        held[0].settimeout(5)
        answer = held[0].recv(65536)
        for sock in held:
            sock.close()
    assert closed == 3, closed
    assert b"MSA|AA|SMPA0001" in answer
    assert "too many open TLS connections" in caplog.text and "limit 2" in caplog.text


def test_one_address_cannot_hold_every_pending_handshake(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    with (
        caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx, max_pending_per_address=2) as srv,
    ):
        socks = [socket.create_connection(("127.0.0.1", srv.port), timeout=5) for _ in range(6)]
        time.sleep(0.5)
        closed = _closed_count(socks)
    assert closed >= 4, closed  # the global cap (64) was never reached
    assert "from one address" in caplog.text and "limit 2" in caplog.text


# R3: SIGTERM takes the same path as Ctrl+C

_SLOW_STORE = (
    "import asyncio\n"
    "from hl7bridge.ingest.pipeline import IngestPipeline\n"
    "_handle = IngestPipeline.handle_frame\n"
    "async def _slow(self, payload):\n"
    "    await asyncio.sleep(1.5)\n"
    "    return await _handle(self, payload)\n"
    "IngestPipeline.handle_frame = _slow\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot deliver SIGTERM to a process")
def test_sigterm_gives_a_busy_handler_its_grace_and_its_ack(certs, tmp_path):
    proc, port = _spawn_listener(certs, tmp_path / "t.db", "127.0.0.1", preamble=_SLOW_STORE)
    data = b""
    try:
        ctx = client_ssl_context(certs["ca"])
        with (
            socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
            ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
        ):
            tls.sendall(mllp.encode_mllp(MSG))
            time.sleep(0.5)  # the frame is in the (slow) handler
            proc.send_signal(signal.SIGTERM)
            while not data.endswith(b"\x1c\r"):
                chunk = tls.recv(65536)
                if not chunk:
                    break
                data += chunk
        out, err = proc.communicate(timeout=20)
    except BaseException:
        proc.kill()
        proc.communicate(timeout=10)
        raise
    assert b"MSA|AA|SMPA0001" in data, err
    assert proc.returncode == 0, err
    assert "listener stopped" in out


# R4: a client still in its handshake at shutdown


def test_shutdown_with_a_client_in_its_handshake_raises_nothing(certs, caplog):
    async def run() -> None:
        server = await _tls_handler_server(certs)
        port = server.sockets[0].getsockname()[1]
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)  # no ClientHello
        await asyncio.sleep(0.3)
        await mllp.shutdown_server(server, grace_s=1.0)
        await asyncio.sleep(0.3)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert "Unhandled exception" not in caplog.text
    assert "AttributeError" not in caplog.text


# R5: a maximum-size frame whose terminator is split across reads


def _decode(*chunks: bytes) -> list[object]:
    async def run() -> list[object]:
        reader = _FakeReader(*chunks, b"")
        return [item async for item in decode_frames(reader)]  # type: ignore[arg-type]

    return asyncio.run(run())


def test_a_maximum_size_frame_with_a_split_terminator_is_a_frame():
    cap = mllp.MAX_FRAME_BYTES
    assert _decode(b"\x0b" + b"a" * cap + b"\x1c", b"\r") == [Frame(b"a" * cap)]
    # The pending FS is decided by the next byte: anything but CR is junk.
    junk = _decode(b"\x0b" + b"a" * cap + b"\x1c", b"x")
    assert all(isinstance(i, JunkBytes) for i in junk)
    assert sum(len(i.data) for i in junk) == cap + 3  # type: ignore[attr-defined]
    # One byte more than the cap is still quarantined, FS or not.
    over = _decode(b"\x0b" + b"a" * (cap + 1) + b"\x1c", b"\r")
    assert not any(isinstance(i, Frame) for i in over)


# R6: a name lookup that never returns


def test_a_stuck_name_lookup_does_not_hold_the_cli_past_the_timeout(monkeypatch, capsys):
    real = socket.getaddrinfo

    def slow(host, *args, **kwargs):
        if host == "slow.test":
            time.sleep(4)
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    start = time.monotonic()
    rc = cli.main(_send_args(2575, "--host", "slow.test", "--timeout", "0.5"))
    elapsed = time.monotonic() - start
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert elapsed < 2.5, elapsed
    assert "timed out" in err and "Never connected" in err


# R7: certificate subject and alternative names only at DEBUG


def test_certificate_subject_and_names_are_not_logged_at_info(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    client_ctx = client_ssl_context(certs["ca"], certs["client_cert"], certs["client_key"])
    with (
        caplog.at_level(logging.INFO, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx) as srv,
    ):
        assert _send(srv.port, client_ctx) == [ACK]
    assert "certificate peer=" in caplog.text  # the fingerprint line stays
    assert "commonName%3Dclient" not in caplog.text and "client.test" not in caplog.text


# R8: the docs do not offer --tls-ca as a cure for the Disallowed store


def test_the_docs_say_neither_mode_consults_the_disallowed_store():
    root = Path(hl7bridge.__file__).resolve().parents[2]
    for name in ("README.md", "docs/cli.md", "docs/threat-model.md"):
        text = " ".join((root / name).read_text(encoding="utf-8").split())
        assert "Neither `--tls` nor `--tls-ca` consults" in text, name
        assert "so use `--tls-ca`" not in text, name


# R9: failed-handshake lines have a listener-wide cap


def test_failed_handshake_lines_are_capped_across_all_peers(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(mllp, "_clock", lambda: now[0])
    err = ssl.SSLError("x")
    with caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"):
        for n in range(200):  # a /64 worth of distinct addresses
            mllp._note_handshake_failure((f"2001:db8::{n:x}", 1), err)
        assert len(caplog.records) <= 25
        now[0] += 61
        mllp._note_handshake_failure(("2001:db8::ffff", 1), err)
        mllp.log_handshake_total()
    assert handshake_failures() == 201
    assert any("suppressed" in r.getMessage() for r in caplog.records)
    assert "in total: 201" in caplog.text


# R10: a reset during the handshake is not called a stall


def test_a_handshake_reset_is_not_reported_as_a_stall(monkeypatch, capsys):
    async def reset(self, *_a, **_k):
        raise ConnectionAbortedError(10053, "aborted by the peer")

    monkeypatch.setattr(asyncio.StreamWriter, "start_tls", reset)
    with TlsServer(None) as srv:
        rc = cli.main(_send_args(srv.port, "--tls", "--timeout", "5"))
    err = capsys.readouterr().err
    assert rc == EXIT_CONNECTION
    assert "no TLS handshake" not in err
    assert "the TLS handshake with" in err and "failed" in err


# Lower: --show-ack-text follows the sign of the ACK, not --expect


def test_show_ack_text_follows_the_sign_of_the_ack(tmp_path, capsys):
    msg = tmp_path / "a.hl7"
    msg.write_bytes(MSG)
    with _scripted_listener(_text_ack) as (port, _seen):
        cli.main(["send", str(msg), "--port", str(port), "--expect", "AE", "--show-ack-text"])
    out = capsys.readouterr().out
    assert "MSA-3: rejected MRN 12345678 Doe" in out  # an expected AE still shows its text

    def aa_with_text(payload: bytes) -> bytes:
        control_id = payload.split(b"\r")[0].split(b"|")[9]
        return mllp.encode_mllp(_HEADER + b"MSA|AA|" + control_id + b"|free text 999\r")

    with _scripted_listener(aa_with_text) as (port, _seen):
        cli.main(["send", str(msg), "--port", str(port), "--expect", "AE", "--show-ack-text"])
    out = capsys.readouterr().out
    assert "AA (" in out and "free text 999" not in out  # an AA is not a negative ACK


# -- Round 6 (S1 to S9) -----------------------------------------------------------------


def _fill_quarantine(db: Path, *payloads: bytes) -> None:
    from hl7bridge.config import HospitalRegistry
    from hl7bridge.ingest import IngestPipeline
    from hl7bridge.store import connect

    conn = connect(db)
    try:
        pipeline = IngestPipeline(HospitalRegistry.load_dir(BUNDLED_PROFILES), conn)
        for payload in payloads:
            pipeline.handle_bytes(payload)
    finally:
        conn.close()


# S2: replay sends a row with no control id; only send refuses one


def test_replay_sends_rows_without_a_control_id_and_reports_the_ar(tmp_path, capsys):
    source = tmp_path / "source.db"
    _fill_quarantine(source, _with_control_id(b""), b"not hl7 at all")
    with _Listener(tmp_path / "target.db") as listener:
        rc = cli.main(
            ["replay", "--from", "quarantine", "--db", str(source), "--port", str(listener.port)]
        )
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS, captured.err
    assert "not a valid ACK" not in captured.err and "nothing sent" not in captured.err
    assert "quarantine reason: missing_control_id" in captured.out
    assert "quarantine reason: parse_error" in captured.out
    assert "got 2 ACK(s)" in captured.out and "  AR: 2" in captured.out
    assert _store_rows(tmp_path / "target.db") == (0, 2)


# S3: one normalization, the listener's, on both sides


def test_leading_whitespace_and_an_escaped_control_id_round_trip(tmp_path):
    payload = b" \t\r\n" + _with_control_id(b"WS\\F\\01")
    with _Listener(tmp_path / "s.db") as listener:
        acks = asyncio.run(send_messages("127.0.0.1", listener.port, [payload], ack_timeout_s=5))
    assert len(acks) == 1 and b"\rMSA|AA|WS\\F\\01" in acks[0]
    assert _store_rows(tmp_path / "s.db") == (1, 0)


@pytest.mark.parametrize(
    "control_id", [b"\\X01\\", b"\\X01\\ \\X0D\\", b"UNKNOWN", b"AB\xfe", b"\\.sp5\\A"]
)
def test_send_refuses_a_control_id_no_ack_can_answer(control_id, tmp_path, capsys):
    msg = tmp_path / "a.hl7"
    msg.write_bytes(_with_control_id(control_id))
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(msg), "--port", str(listener.port)])
    captured = capsys.readouterr()
    assert rc == EXIT_USAGE, captured.err
    assert "MSH-10" in captured.err and "nothing sent" in captured.err
    assert _store_rows(tmp_path / "s.db") == (0, 0)


def test_send_refuses_two_messages_that_share_a_control_id(tmp_path, capsys):
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "a.hl7").write_bytes(_with_control_id(b"DUP01"))
    other = _with_control_id(b"\\X01\\DUP01").replace(b"PID|", b"PID||", 1)
    (folder / "b.hl7").write_bytes(other)
    with _Listener(tmp_path / "s.db") as listener:
        rc = cli.main(["send", str(folder), "--port", str(listener.port)])
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE, captured.err
        assert "same MSH-10" in captured.err and "nothing sent" in captured.err
        # The same bytes again (a retry, --repeat) are not ambiguous.
        rc = cli.main(["send", str(folder / "a.hl7"), "--port", str(listener.port)])
        rc2 = cli.main(
            ["send", str(folder / "a.hl7"), "--port", str(listener.port), "--repeat", "2"]
        )
    captured = capsys.readouterr()
    assert rc == rc2 == EXIT_OK, captured.err
    assert _store_rows(tmp_path / "s.db") == (1, 0)


# S4: MSA-1 first, UNKNOWN only when negative, one MSH and one MSA, bounded fields


@pytest.mark.parametrize(
    ("payload", "ack"),
    [
        (_with_control_id(b"UNKNOWN"), _HEADER + b"MSA|AA|UNKNOWN\r"),
        (_with_control_id(b"UNKNOWN"), _HEADER + b"MSA|CA|UNKNOWN\r"),
        (MSG, _HEADER + b"MSA||SMPA0001\r"),
        (MSG, _HEADER + b"MSA|OK|SMPA0001\r"),
        (MSG, _HEADER + b"MSH|^~\\&|R|F|S|F|20261007||ORU^R01|2|P|2.5\rMSA|AA|SMPA0001\r"),
        (MSG, _HEADER + b"MSA|AA|\\.sp5\\SMPA0001\r"),
        (MSG, _HEADER + b"MSA|AA|\\.sp1x\\SMPA0001\r"),
        (_with_control_id(b"AB\xfe"), _HEADER + b"MSA|AA|AB\xff\r"),
        (_with_control_id(b"\\X01\\"), _HEADER + b"MSA|AA|\r"),
    ],
    ids=[
        "aa_unknown",
        "ca_unknown",
        "empty_msa1",
        "not_a_code",
        "second_msh",
        "expanding_escape",
        "bad_escape_count",
        "lossy_bytes",
        "empty_after_decoding",
    ],
)
def test_check_ack_refuses(payload, ack):
    with pytest.raises(AckProtocolError):
        mllp.check_ack(payload, ack)


def test_an_ack_text_with_a_bad_escape_is_shown_without_a_crash(tmp_path, capsys):
    def ae(payload: bytes) -> bytes:
        control_id = payload.split(b"\r")[0].split(b"|")[9]
        return mllp.encode_mllp(_HEADER + b"MSA|AE|" + control_id + b"|x\\.sp1x\\y\r")

    msg = tmp_path / "a.hl7"
    msg.write_bytes(MSG)
    with _scripted_listener(ae) as (port, _seen):
        rc = cli.main(["send", str(msg), "--port", str(port), "--show-ack-text"])
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS, captured.err
    assert ": AE (" in captured.out


# S5: the library refuses a message without a control id before writing it


def test_send_messages_refuses_a_message_without_a_control_id_before_writing():
    later = _with_control_id(b"LATER01")
    with (
        _scripted_listener(_good, _good, _good) as (port, seen),
        pytest.raises(ValueError, match=r"MSH-10.*nothing more sent"),
    ):
        asyncio.run(send_messages("127.0.0.1", port, [MSG, _with_control_id(b""), later]))
    assert seen == [MSG]  # the one before it went; it and the rest did not


# S6: one address cannot hold every open connection; the limits are checked


def test_one_address_cannot_hold_every_open_connection(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"])
    client_ctx = client_ssl_context(certs["ca"])

    def tls(source: str = "127.0.0.1") -> ssl.SSLSocket:
        raw = socket.create_connection(("127.0.0.1", srv.port), 5, (source, 0))
        return client_ctx.wrap_socket(raw, server_hostname="127.0.0.1")

    with (
        caplog.at_level(NOTICE, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx, max_connections_per_address=2) as srv,
    ):
        held = [tls(), tls()]
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        time.sleep(0.3)
        refused = _closed_count([extra])
        try:
            held.append(tls("127.0.0.2"))  # another address still gets in
        except OSError:
            if sys.platform.startswith("linux"):
                raise  # all of 127/8 is loopback there
        held.pop(0).close()  # a slot freed is a slot reused
        time.sleep(0.3)
        again = tls()
        again.sendall(mllp.encode_mllp(MSG))
        again.settimeout(5)
        answer = again.recv(65536)
        for sock in (*held, again):
            sock.close()
    assert refused == 1
    assert b"MSA|AA|SMPA0001" in answer
    assert "open TLS connections from one address" in caplog.text and "limit 2" in caplog.text


@pytest.mark.parametrize(
    "limit",
    [
        "max_connections",
        "max_connections_per_address",
        "max_pending_handshakes",
        "max_pending_per_address",
    ],
)
def test_a_connection_limit_below_one_is_refused(certs, limit):
    with pytest.raises(ValueError, match=limit):
        asyncio.run(_tls_handler_server(certs, **{limit: 0}))


def test_listen_passes_the_per_address_limits(certs, tmp_path, monkeypatch, capsys):
    seen: dict[str, object] = {}

    async def fake(host, port, handler, on_junk, **kw):
        seen.update(kw)
        return _FakeServer(KeyboardInterrupt())

    monkeypatch.setattr(cli, "serve", fake)
    rc = cli.main(
        [
            "listen",
            "--db",
            str(tmp_path / "b.db"),
            "--tls-cert",
            certs["server_cert"],
            "--tls-key",
            certs["server_key"],
            "--max-connections-per-address",
            "3",
            "--max-pending-per-address",
            "2",
        ]
    )
    capsys.readouterr()
    assert rc == EXIT_OK
    assert seen["max_connections_per_address"] == 3 and seen["max_pending_per_address"] == 2


# S7: no transport after start_tls; a second SIGTERM during the shutdown


def _no_transport(loop: asyncio.AbstractEventLoop) -> None:
    async def start_tls(*_a, **_k):
        return None  # what the loop returns when the connection went during the upgrade

    loop.start_tls = start_tls  # type: ignore[method-assign]


def test_a_listener_upgrade_with_no_transport_is_a_failed_handshake(certs, caplog):
    async def run() -> bytes:
        _no_transport(asyncio.get_running_loop())
        server = await _tls_handler_server(certs)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            return await asyncio.wait_for(reader.read(10), 5)
        finally:
            writer.close()
            server.close()
            await server.wait_closed()

    with caplog.at_level(logging.DEBUG):
        data = asyncio.run(run())
    assert data == b""
    assert handshake_failures() == 1
    assert "Unhandled exception" not in caplog.text and "AttributeError" not in caplog.text


def test_a_sender_upgrade_with_no_transport_is_a_handshake_error(certs):
    async def run() -> None:
        _no_transport(asyncio.get_running_loop())
        await send_messages(
            "127.0.0.1", srv.port, [MSG], ssl_context=client_ssl_context(certs["ca"])
        )

    with TlsServer(None) as srv, pytest.raises(TlsHandshakeError):
        asyncio.run(run())


_SLOW_STORE_MARKED = (
    "import asyncio, os\n"
    "from hl7bridge.ingest.pipeline import IngestPipeline\n"
    "_handle = IngestPipeline.handle_frame\n"
    "async def _slow(self, payload):\n"
    "    open(os.environ['HL7BRIDGE_TEST_ENTERED'], 'w').close()\n"
    "    await asyncio.sleep(2.0)\n"
    "    return await _handle(self, payload)\n"
    "IngestPipeline.handle_frame = _slow\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot deliver SIGTERM to a process")
def test_a_second_sigterm_does_not_cut_the_grace_short(certs, tmp_path, monkeypatch):
    entered = tmp_path / "entered"
    monkeypatch.setenv("HL7BRIDGE_TEST_ENTERED", str(entered))
    proc, port = _spawn_listener(certs, tmp_path / "t.db", "127.0.0.1", preamble=_SLOW_STORE_MARKED)
    data = b""
    try:
        ctx = client_ssl_context(certs["ca"])
        with (
            socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
            ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
        ):
            tls.sendall(mllp.encode_mllp(MSG))
            assert _wait_for(entered.exists)  # the frame is in the (slow) handler
            proc.send_signal(signal.SIGTERM)
            time.sleep(0.3)
            proc.send_signal(signal.SIGTERM)  # the shutdown is under way
            while not data.endswith(b"\x1c\r"):
                chunk = tls.recv(65536)
                if not chunk:
                    break
                data += chunk
        out, err = proc.communicate(timeout=20)
    except BaseException:
        proc.kill()
        proc.communicate(timeout=10)
        raise
    assert b"MSA|AA|SMPA0001" in data, err
    assert proc.returncode == 0, err
    assert "listener stopped" in out


# S8: the issuer, which can repeat a self-issued subject, is logged at DEBUG only


def test_the_issuer_is_logged_at_debug_only(certs, caplog):
    server_ctx = server_ssl_context(certs["server_cert"], certs["server_key"], certs["ca"])
    client_ctx = client_ssl_context(certs["ca"], certs["client_cert"], certs["client_key"])
    with (
        caplog.at_level(logging.DEBUG, logger="hl7bridge.ingest.mllp"),
        TlsServer(server_ctx) as srv,
    ):
        assert _send(srv.port, client_ctx) == [ACK]
    shown = [r.getMessage() for r in caplog.records if r.levelno > logging.DEBUG]
    assert any("certificate peer=" in m for m in shown)
    assert not any("issuer=" in m for m in shown)
    details = [r.getMessage() for r in caplog.records if "certificate details" in r.getMessage()]
    assert len(details) == 1 and "issuer=" in details[0]


# A socket that cannot be made for one lookup result does not end the connect


def test_a_socket_failure_on_one_address_tries_the_next(monkeypatch):
    import types

    real = socket

    def lookup(host, port, *args, **kwargs):
        return [
            (real.AF_INET6, real.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
            (real.AF_INET, real.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    def make(family, kind, proto):
        if family == real.AF_INET6:
            raise OSError(errno.EAFNOSUPPORT, "address family not supported")
        return real.socket(family, kind, proto)

    fake = types.SimpleNamespace(getaddrinfo=lookup, SOCK_STREAM=real.SOCK_STREAM, socket=make)
    with TlsServer(None) as srv:
        monkeypatch.setattr(mllp, "socket", fake)
        assert asyncio.run(send_messages("localhost", srv.port, [MSG], ack_timeout_s=5)) == [ACK]


# -- Round 7 ----------------------------------------------------------------------------


# send_messages streams: an iterator is read one message at a time


def test_send_messages_reads_an_iterator_one_message_at_a_time():
    acks: list[bytes] = []
    taken: list[int] = []

    def stream():
        for n in range(3):
            taken.append(len(acks))  # ACKs already in when message n is taken
            yield _with_control_id(f"STREAM{n}".encode())

    with _scripted_listener(_good, _good, _good) as (port, seen):
        asyncio.run(send_messages("127.0.0.1", port, stream(), acks=acks))
    assert taken == [0, 1, 2] and len(seen) == len(acks) == 3


# python-hl7 asserts on an MSH whose encoding characters repeat a separator

_REPEATED_ENCODING = b"MSH|^^~&|A|B|C|D|20261121||ADT^A01|X1|P|2.5"


def test_repeated_encoding_characters_are_a_parse_error():
    from hl7bridge.errors import HL7ParseError
    from hl7bridge.ingest.parser import control_id_of, parse_hl7

    with pytest.raises(HL7ParseError):
        parse_hl7(_REPEATED_ENCODING.decode())
    assert control_id_of(_REPEATED_ENCODING) == ""


def test_the_listener_rejects_and_replay_reports_repeated_encoding_characters(tmp_path, capsys):
    source = tmp_path / "source.db"
    _fill_quarantine(source, _REPEATED_ENCODING + b"\r", b"not hl7 at all")
    assert _store_rows(source) == (0, 2)
    with _Listener(tmp_path / "target.db") as listener:
        rc = cli.main(
            ["replay", "--from", "quarantine", "--db", str(source), "--port", str(listener.port)]
        )
    captured = capsys.readouterr()
    assert rc == EXIT_FINDINGS, captured.err
    assert "not a valid ACK" not in captured.err
    assert "got 2 ACK(s)" in captured.out and "  AR: 2" in captured.out
    assert _store_rows(tmp_path / "target.db") == (0, 2)


# A Ctrl+C while the direct workers are joined closes nothing under them


def test_a_ctrl_c_during_the_worker_join_leaves_the_client_and_log_open(tmp_path, monkeypatch):
    import argparse

    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    class Handler:
        handle_frame = handle_junk = None

        def __init__(self, *_a) -> None:
            pass

        def close(self, timeout: float) -> bool:
            raise KeyboardInterrupt

    for name, value in {
        "_named_fhir_base": lambda *_a: "http://127.0.0.1:9/fhir",
        "_run_id": lambda _a: "r1",
        "_registry": lambda _a: None,
        "check_profiles_for_direct": lambda _r: None,
        "_load_hooks": lambda _a: None,
        "_lab_tag": lambda _r: ("urn:x", "y"),
        "RunScope": lambda *_a: None,
        "_fhir_headers": dict,
        "_warn_plain_http": lambda *_a: None,
        "DirectLog": lambda _p: Resource("log"),
        "HapiClient": lambda *_a, **_k: Resource("client"),
        "_require_metadata": lambda *_a: None,
        "DirectHandler": Handler,
        "_serve_forever": lambda *_a: 0,
    }.items():
        monkeypatch.setattr(cli, name, value)
    args = argparse.Namespace(
        mapping_module=None, host="127.0.0.1", db=str(tmp_path / "d.db"), fhir_timeout=5.0
    )
    with pytest.raises(KeyboardInterrupt):
        cli._listen_direct(args, None)
    assert closed == []


# MSA-1 is exactly one ACK code: the whole field


@pytest.mark.parametrize(
    "msa1", [b"AA~AR", b"AA^AE", b"AA&x", b"AA~AA", b" AA"], ids=lambda v: v.decode()
)
def test_msa1_must_be_exactly_one_ack_code(msa1):
    with pytest.raises(AckProtocolError, match="MSA-1"):
        mllp.check_ack(MSG, _HEADER + b"MSA|" + msa1 + b"|SMPA0001\r")
    mllp.check_ack(MSG, _HEADER + b"MSA|AA|SMPA0001\r")
