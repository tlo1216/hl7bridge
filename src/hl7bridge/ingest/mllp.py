"""MLLP framing over TCP.

Frame format: <VT=0x0B> payload <FS=0x1C> <CR=0x0D>.

Junk bytes between frames are never silently dropped: the codec yields them
as explicit ``JunkBytes`` items so the pipeline can quarantine and count them
(fail loudly: a degraded path must never look healthy).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass

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
) -> asyncio.Server:
    """Start the MLLP listener. Caller owns the returned server's lifetime."""

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_connection(reader, writer, handler, on_junk)

    server = await asyncio.start_server(connection, host, port)
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("mllp listening on %s", sockets)
    return server


async def send_messages(
    host: str,
    port: int,
    payloads: Iterable[bytes],
    *,
    ack_timeout_s: float = 10.0,
    acks: list[bytes] | None = None,
    interval_s: float = 0.0,
) -> list[bytes]:
    """Send each payload as one MLLP frame and collect the ACK responses.

    A missing or late ACK raises TimeoutError; silence is never success.
    Pass ``acks`` to keep the ACKs that did arrive when that happens.
    ``interval_s`` is the pause between one ACK and the next send.
    """
    reader, writer = await asyncio.open_connection(host, port)
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
