"""MLLP framing over TCP.

Frame format: <VT=0x0B> payload <FS=0x1C> <CR=0x0D>.

Junk bytes between frames are never silently dropped: the codec yields them
as explicit ``JunkBytes`` items so the pipeline can quarantine and count them
(fail loudly: a degraded path must never look healthy).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..errors import TlsConfigError

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
        chunk = await reader.read(65536)
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


def _load_chain(ctx: ssl.SSLContext, certfile: str, keyfile: str, what: str) -> None:
    """Load a certificate and its key. Errors name the file, never the key."""
    for path, kind in ((certfile, "certificate"), (keyfile, "private key")):
        if not Path(path).is_file():
            raise TlsConfigError(f"cannot read the TLS {what} {kind} file {path}.")
    try:
        ctx.load_cert_chain(certfile, keyfile)
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
    if cafile is not None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies chain and host name
        _load_ca(ctx, cafile)
    else:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if (certfile is None) != (keyfile is None):
        raise TlsConfigError("a TLS client certificate needs both the certificate and its key.")
    if certfile is not None and keyfile is not None:
        _load_chain(ctx, certfile, keyfile, "client")
    return ctx


Handler = Callable[[bytes], Awaitable[bytes]]


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
    except ConnectionResetError:
        log.warning("mllp connection reset peer=%s", peer)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
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
        await _handle_connection(reader, writer, handler, on_junk)

    server = await asyncio.start_server(
        connection,
        host,
        port,
        ssl=ssl_context,
        ssl_handshake_timeout=HANDSHAKE_TIMEOUT_S if ssl_context else None,
    )
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
    reader, writer = await asyncio.open_connection(
        host,
        port,
        ssl=ssl_context,
        ssl_handshake_timeout=max(ack_timeout_s, 1.0) if ssl_context else None,
    )
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
        await writer.wait_closed()
    return acks


async def _read_one_frame(reader: asyncio.StreamReader) -> bytes:
    async for item in decode_frames(reader):
        if isinstance(item, Frame):
            return item.payload
        raise ValueError(f"expected MLLP frame, got {len(item.data)} junk bytes")
    raise ConnectionError("connection closed before ACK frame arrived")
