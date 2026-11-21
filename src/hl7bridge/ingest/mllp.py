"""MLLP framing over TCP.

Frame format: <VT=0x0B> payload <FS=0x1C> <CR=0x0D>.

Junk bytes between frames are never silently dropped: the codec yields them
as explicit ``JunkBytes`` items so the pipeline can quarantine and count them
(fail loudly: a degraded path must never look healthy).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConnectError, TlsConfigError, TlsHandshakeError
from ..logsafety import NOTICE, printable

log = logging.getLogger(__name__)

START_BLOCK = b"\x0b"
END_BLOCK = b"\x1c\x0d"

# One frame must fit comfortably in memory; a report message is a few KB.
MAX_FRAME_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class Frame:
    payload: bytes


@dataclass(frozen=True, slots=True)
class JunkBytes:
    """Bytes received outside MLLP framing (protocol violation)."""

    data: bytes


def encode_mllp(payload: bytes) -> bytes:
    return START_BLOCK + payload + END_BLOCK


async def decode_frames(reader: asyncio.StreamReader) -> AsyncIterator[Frame | JunkBytes]:
    """Yield frames (and any inter-frame junk) until EOF.

    An unterminated trailing frame at EOF is yielded as JunkBytes: the sender
    hung up mid-message and that must be visible, not discarded.
    """
    buffer = b""
    while True:
        try:
            chunk = await reader.read(65536)
        except (ConnectionError, ssl.SSLError, TimeoutError):
            # The connection broke mid-frame: the bytes read so far are not
            # lost silently, the caller quarantines them as junk.
            if buffer:
                yield JunkBytes(buffer)
            raise
        if not chunk:
            if buffer:
                yield JunkBytes(buffer)
            return
        buffer += chunk
        while True:
            start = buffer.find(START_BLOCK)
            if start == -1:
                break
            if start > 0:
                yield JunkBytes(buffer[:start])
                buffer = buffer[start:]
            end = buffer.find(END_BLOCK, 1)
            if end == -1:
                break  # incomplete frame; wait for more bytes
            yield Frame(buffer[1:end])
            buffer = buffer[end + len(END_BLOCK) :]
        # Size guard AFTER extraction: at this point the residual buffer holds
        # only an incomplete frame or start-block-less junk, so complete valid
        # frames can never be dumped by the guard (regression-tested).
        if len(buffer) > MAX_FRAME_BYTES:
            yield JunkBytes(buffer)
            buffer = b""


HANDSHAKE_TIMEOUT_S = 10.0


class _EncryptedKey(Exception):
    """Raised by the password callback so an encrypted key never prompts."""


def _refuse_password() -> bytes:
    raise _EncryptedKey


def _load_chain(ctx: ssl.SSLContext, certfile: str, keyfile: str, what: str) -> None:
    """Load a certificate and its key. Errors name the file, never the key."""
    for path, kind in ((certfile, "certificate"), (keyfile, "private key")):
        if not Path(path).is_file():
            raise TlsConfigError(f"cannot read the TLS {what} {kind} file {path}.")
    try:
        ctx.load_cert_chain(certfile, keyfile, password=_refuse_password)
    except _EncryptedKey:
        raise TlsConfigError(
            f"the TLS {what} private key {keyfile} is encrypted. Give an unencrypted key"
            " (hl7bridge never asks for a passphrase)."
        ) from None
    except PermissionError:
        raise TlsConfigError(
            f"cannot read the TLS {what} certificate {certfile} or key {keyfile}:"
            " permission denied."
        ) from None
    except (ssl.SSLError, OSError):
        raise TlsConfigError(
            f"the TLS {what} certificate {certfile} and key {keyfile} could not be loaded:"
            " they are not valid PEM, or the key does not belong to the certificate."
        ) from None


def _load_ca(ctx: ssl.SSLContext, cafile: str) -> None:
    if not Path(cafile).is_file():
        raise TlsConfigError(f"cannot read the TLS CA file {cafile}.")
    try:
        ctx.load_verify_locations(cafile=cafile)
    except (ssl.SSLError, OSError):
        raise TlsConfigError(f"the TLS CA file {cafile} holds no usable PEM certificate.") from None


def server_ssl_context(certfile: str, keyfile: str, cafile: str | None = None) -> ssl.SSLContext:
    """TLS 1.2 or newer. With ``cafile`` every client must present a
    certificate that chains to it; without it, clients are not asked."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_RENEGOTIATION
    _load_chain(ctx, certfile, keyfile, "server")
    if cafile is not None:
        _load_ca(ctx, cafile)
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def client_ssl_context(
    cafile: str | None = None, certfile: str | None = None, keyfile: str | None = None
) -> ssl.SSLContext:
    """TLS 1.2 or newer, server certificate and host name always checked.
    ``cafile`` is the only trust anchor when given; otherwise the system store."""
    # Both paths start from the same bare context, so the extras of
    # create_default_context() (such as SSLKEYLOGFILE) apply to neither.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies chain and host name
    if cafile is not None:
        _load_ca(ctx, cafile)
    else:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_RENEGOTIATION
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if (certfile is None) != (keyfile is None):
        raise TlsConfigError("a TLS client certificate needs both the certificate and its key.")
    if certfile is not None and keyfile is not None:
        _load_chain(ctx, certfile, keyfile, "client")
    return ctx


Handler = Callable[[bytes], Awaitable[bytes]]

_handshake_failures = 0

# Failed handshakes are logged once per peer address per window; the rest of
# the window is counted and reported in one summary line, so a scanner cannot
# fill the log. The total is reported at shutdown (see ``log_handshake_total``).
HANDSHAKE_LOG_WINDOW_S = 60.0
_clock = time.monotonic
_windows: dict[str, tuple[float, int]] = {}  # peer host -> (window start, suppressed)


def handshake_failures() -> int:
    """Failed TLS handshakes seen by listeners in this process."""
    return _handshake_failures


def reset_handshake_state() -> None:
    """Forget the count and the per-peer windows (for tests)."""
    global _handshake_failures
    _handshake_failures = 0
    _windows.clear()


def _peer_host(peer: object) -> str:
    return str(peer[0]) if isinstance(peer, tuple) and peer else str(peer)


def _handshake_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None) or type(exc).__name__
    detail = getattr(exc, "verify_message", None)
    text = f"{reason}: {detail}" if detail else str(reason)
    return printable(text)


def _note_handshake_failure(peer: object, exc: BaseException) -> None:
    global _handshake_failures
    _handshake_failures += 1
    host = _peer_host(peer)
    now = _clock()
    start, suppressed = _windows.get(host, (None, 0))
    if start is not None and now - start < HANDSHAKE_LOG_WINDOW_S:
        _windows[host] = (start, suppressed + 1)
        return
    if suppressed:
        log.log(
            NOTICE,
            "mllp tls handshake failed %d more time(s) peer=%r in the last %d s",
            suppressed,
            printable(host),
            int(HANDSHAKE_LOG_WINDOW_S),
        )
    _windows[host] = (now, 0)
    log.log(
        NOTICE,
        "mllp tls handshake failed peer=%r reason=%r total=%d",
        printable(peer),
        _handshake_reason(exc),
        _handshake_failures,
    )


def log_handshake_total() -> None:
    """Report suppressed repeats and the total. Call at any listener shutdown."""
    for host, (_start, suppressed) in list(_windows.items()):
        if suppressed:
            log.log(
                NOTICE,
                "mllp tls handshake failed %d more time(s) peer=%r",
                suppressed,
                printable(host),
            )
            _windows[host] = (_clock(), 0)
    if _handshake_failures:
        log.log(NOTICE, "mllp tls handshakes failed in total: %d", _handshake_failures)


def _rdns(value: object) -> str:
    """``commonName=x, organizationName=y`` from a getpeercert name."""
    if not isinstance(value, tuple):
        return ""
    return ", ".join(f"{k}={v}" for rdn in value for k, v in rdn)


def _identity(cert: object) -> dict[str, str]:
    """The identity of a verified client certificate, each value made safe
    for a log line: subject, subject alternative names, serial, issuer."""
    if not isinstance(cert, dict):
        return {}

    sans = ", ".join(f"{k}:{v}" for k, v in cert.get("subjectAltName", ()))
    return {
        "subject": printable(_rdns(cert.get("subject"))),
        "san": printable(sans),
        "serial": printable(cert.get("serialNumber", "")),
        "issuer": printable(_rdns(cert.get("issuer"))),
    }


async def _start_tls(writer: asyncio.StreamWriter, ctx: ssl.SSLContext) -> bool:
    """Upgrade an accepted connection to TLS. False (closed) when it fails."""
    peer = writer.get_extra_info("peername")
    try:
        await writer.start_tls(ctx, ssl_handshake_timeout=HANDSHAKE_TIMEOUT_S)
    except (ssl.SSLError, OSError, TimeoutError) as exc:
        _note_handshake_failure(peer, exc)
        writer.close()
        return False
    ident = _identity(writer.get_extra_info("peercert"))
    if ident:
        # A verified client always gets this line, whatever its subject.
        log.log(
            NOTICE,
            "mllp client certificate peer=%r subject=%r san=%r serial=%r issuer=%r",
            printable(peer),
            ident["subject"],
            ident["san"],
            ident["serial"],
            ident["issuer"],
        )
    return True


async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handler: Handler,
    on_junk: Callable[[bytes], Awaitable[None]],
) -> None:
    peer = writer.get_extra_info("peername")
    log.info("mllp connection opened peer=%s", peer)
    try:
        async for item in decode_frames(reader):
            if isinstance(item, JunkBytes):
                log.warning("mllp junk bytes peer=%s length=%d", peer, len(item.data))
                await on_junk(item.data)
                continue
            response = await handler(item.payload)
            writer.write(encode_mllp(response))
            await writer.drain()
    except (ConnectionError, ssl.SSLError, TimeoutError) as exc:
        log.warning("mllp connection broke peer=%s error=%s", peer, type(exc).__name__)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            log.warning("mllp close race peer=%s", peer)
        log.info("mllp connection closed peer=%s", peer)


async def serve(
    host: str,
    port: int,
    handler: Handler,
    on_junk: Callable[[bytes], Awaitable[None]],
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> asyncio.Server:
    """Start the MLLP listener. Caller owns the returned server's lifetime.
    Pass ``ssl_context`` (see ``server_ssl_context``) for MLLP over TLS."""

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # TLS starts here, not in start_server, so a failed handshake is seen
        # with its peer address and can be logged and counted.
        if ssl_context is not None and not await _start_tls(writer, ssl_context):
            return
        await _handle_connection(reader, writer, handler, on_junk)

    server = await asyncio.start_server(connection, host, port)
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("mllp listening on %s%s", sockets, " (tls)" if ssl_context else "")
    return server


async def send_messages(
    host: str,
    port: int,
    payloads: Iterable[bytes],
    *,
    ack_timeout_s: float = 10.0,
    acks: list[bytes] | None = None,
    interval_s: float = 0.0,
    ssl_context: ssl.SSLContext | None = None,
) -> list[bytes]:
    """Send each payload as one MLLP frame and collect the ACK responses.

    A missing or late ACK raises TimeoutError; silence is never success.
    Pass ``acks`` to keep the ACKs that did arrive when that happens.
    ``interval_s`` is the pause between one ACK and the next send.
    Pass ``ssl_context`` (see ``client_ssl_context``) for MLLP over TLS; the
    server's certificate must be valid for ``host``.
    """
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as exc:  # refused, no route, unknown host, timed out, several of those
        raise ConnectError(exc) from exc
    if ssl_context is not None:
        # The TCP connection is made; from here a failure is a TLS failure.
        try:
            await writer.start_tls(
                ssl_context,
                server_hostname=host,
                ssl_handshake_timeout=max(ack_timeout_s, 1.0),
                ssl_shutdown_timeout=min(ack_timeout_s, 5.0),
            )
        except (ssl.SSLError, ConnectionError, TimeoutError) as exc:
            writer.close()
            # Nothing has been written yet: the connection never got going.
            raise TlsHandshakeError(exc) from exc
    if acks is None:
        acks = []
    try:
        for i, payload in enumerate(payloads):
            if i and interval_s > 0:
                await asyncio.sleep(interval_s)
            writer.write(encode_mllp(payload))
            await writer.drain()
            ack = await asyncio.wait_for(_read_one_frame(reader), timeout=ack_timeout_s)
            acks.append(ack)
    finally:
        writer.close()
        with contextlib.suppress(OSError):  # a slow TLS shutdown must not mask the result
            await writer.wait_closed()
    return acks


async def _read_one_frame(reader: asyncio.StreamReader) -> bytes:
    async for item in decode_frames(reader):
        if isinstance(item, Frame):
            return item.payload
        raise ValueError(f"expected MLLP frame, got {len(item.data)} junk bytes")
    raise ConnectionError("connection closed before ACK frame arrived")
