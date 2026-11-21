"""MLLP framing over TCP.

Frame format: <VT=0x0B> payload <FS=0x1C> <CR=0x0D>.

Junk bytes between frames are never silently dropped: the codec yields them
as explicit ``JunkBytes`` items so the pipeline can quarantine and count them
(fail loudly: a degraded path must never look healthy).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import socket
import ssl
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..errors import (
    AckProtocolError,
    ConnectError,
    HL7ParseError,
    TlsConfigError,
    TlsHandshakeError,
)
from ..logsafety import NOTICE, printable
from .ack import ack_value
from .parser import control_id_of, get_field, get_segment_count, parse_hl7

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
            if end - 1 > MAX_FRAME_BYTES:
                # A terminated frame over the cap is quarantined whole, never
                # handed on, even when its terminator arrived in the same read.
                yield JunkBytes(buffer[: end + len(END_BLOCK)])
            else:
                yield Frame(buffer[1:end])
            buffer = buffer[end + len(END_BLOCK) :]
        # Size guard AFTER extraction: at this point the residual buffer holds
        # only an incomplete frame or start-block-less junk, so complete valid
        # frames can never be dumped by the guard (regression-tested). A frame
        # may hold MAX_FRAME_BYTES of payload; its start block is not counted,
        # nor an FS at the end, whose CR may come in the next read.
        limit = (
            MAX_FRAME_BYTES
            + (1 if buffer.startswith(START_BLOCK) else 0)
            + (1 if buffer.endswith(END_BLOCK[:1]) else 0)
        )
        if len(buffer) > limit:
            yield JunkBytes(buffer)
            buffer = b""


HANDSHAKE_TIMEOUT_S = 10.0


class _EncryptedKey(Exception):
    """Raised by the password callback so an encrypted key never prompts."""


def _refuse_password() -> bytes:
    raise _EncryptedKey


def _is_file(path: str) -> bool:
    """False for a missing file and for a value that cannot be a path at all
    (for example PEM text in an environment variable)."""
    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


def _load_chain(ctx: ssl.SSLContext, certfile: str, keyfile: str, what: str) -> None:
    """Load a certificate and its key. Errors name the flag, never its value:
    a value can be key text or hold line breaks, and must not reach a log."""
    for path, flag in ((certfile, "--tls-cert"), (keyfile, "--tls-key")):
        if not _is_file(path):
            raise TlsConfigError(f"cannot read the TLS {what} file given with {flag}.")
    try:
        ctx.load_cert_chain(certfile, keyfile, password=_refuse_password)
    except _EncryptedKey:
        raise TlsConfigError(
            f"the TLS {what} private key given with --tls-key is encrypted. Give an"
            " unencrypted key (hl7bridge never asks for a passphrase)."
        ) from None
    except PermissionError:
        raise TlsConfigError(
            f"cannot read the TLS {what} certificate or key (--tls-cert, --tls-key):"
            " permission denied."
        ) from None
    except (ssl.SSLError, OSError, ValueError):
        raise TlsConfigError(
            f"the TLS {what} certificate and key (--tls-cert, --tls-key) could not be"
            " loaded: they are not valid PEM, or the key does not belong to the certificate."
        ) from None


def _load_ca(ctx: ssl.SSLContext, cafile: str) -> None:
    if not _is_file(cafile):
        raise TlsConfigError("cannot read the TLS CA file given with --tls-ca.")
    try:
        ctx.load_verify_locations(cafile=cafile)
    except (ssl.SSLError, OSError, ValueError):
        raise TlsConfigError(
            "the file given with --tls-ca holds no usable PEM certificate."
        ) from None


def server_ssl_context(certfile: str, keyfile: str, cafile: str | None = None) -> ssl.SSLContext:
    """TLS 1.2 or newer. With ``cafile`` every client must present a
    certificate that chains to it; without it, clients are not asked."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_RENEGOTIATION
    _load_chain(ctx, certfile, keyfile, "server")
    if cafile is not None:
        _load_ca(ctx, cafile)
        # An issuing CA given alone is a trust anchor, as an operator expects.
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def client_ssl_context(
    cafile: str | None = None, certfile: str | None = None, keyfile: str | None = None
) -> ssl.SSLContext:
    """TLS 1.2 or newer, server certificate and host name always checked.
    ``cafile`` is the only trust anchor when given (an issuing CA alone is
    enough: partial-chain verification is on only then); otherwise the system
    store. Names are matched against the certificate's alternative names only,
    never its common name. On Windows the system store is the CA and ROOT
    stores; the Disallowed store is consulted in neither case (see docs/cli.md)."""
    # Both paths start from the same bare context, so the extras of
    # create_default_context() (such as SSLKEYLOGFILE) apply to neither.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies chain and host name
    if cafile is not None:
        _load_ca(ctx, cafile)
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    else:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    ctx.hostname_checks_common_name = False  # RFC 9525: the SAN only
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
# Peer host -> (window start, suppressed), least recently seen first. The
# table is fixed in size: past the cap the oldest peer is forgotten (and its
# suppressed count logged), so address churn cannot grow memory.
HANDSHAKE_PEER_TABLE_MAX = 1024
_windows: OrderedDict[str, tuple[float, int]] = OrderedDict()
# Across all peers, at most this many handshake failure lines a window; the
# rest are counted and reported in one line, so address churn (an IPv6 /64,
# spoofed sources) cannot fill the log either.
HANDSHAKE_LOG_LINES_MAX = 20
_lines_window_at: float | None = None
_lines_used = 0
_lines_dropped = 0


def handshake_failures() -> int:
    """Failed TLS handshakes seen by listeners in this process."""
    return _handshake_failures


def reset_handshake_state() -> None:
    """Forget the count and the per-peer windows (for tests)."""
    global _handshake_failures, _refused_pending, _refused_log_at
    global _lines_window_at, _lines_used, _lines_dropped
    _handshake_failures = 0
    _refused_pending = 0
    _refused_log_at = None
    _lines_window_at, _lines_used, _lines_dropped = None, 0, 0
    _windows.clear()


def _report_dropped_lines() -> None:
    global _lines_dropped
    if _lines_dropped:
        log.log(
            NOTICE,
            "mllp tls handshake failure lines suppressed: %d (all peers, log limit %d a %d s"
            " window; the total counts them)",
            _lines_dropped,
            HANDSHAKE_LOG_LINES_MAX,
            int(HANDSHAKE_LOG_WINDOW_S),
        )
        _lines_dropped = 0


def _handshake_notice(msg: str, *args: object) -> None:
    """One handshake failure line, within the listener-wide budget."""
    global _lines_window_at, _lines_used, _lines_dropped
    now = _clock()
    if _lines_window_at is None or now - _lines_window_at >= HANDSHAKE_LOG_WINDOW_S:
        _report_dropped_lines()
        _lines_window_at, _lines_used = now, 0
    if _lines_used >= HANDSHAKE_LOG_LINES_MAX:
        _lines_dropped += 1
        return
    _lines_used += 1
    log.log(NOTICE, msg, *args)


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
        _windows.move_to_end(host)
        return
    if suppressed:
        _handshake_notice(
            "mllp tls handshake failed %d more time(s) peer=%r in the last %d s",
            suppressed,
            printable(host),
            int(HANDSHAKE_LOG_WINDOW_S),
        )
    _windows[host] = (now, 0)
    _windows.move_to_end(host)
    while len(_windows) > HANDSHAKE_PEER_TABLE_MAX:
        old, (_old_start, old_suppressed) = _windows.popitem(last=False)
        if old_suppressed:
            _handshake_notice(
                "mllp tls handshake failed %d more time(s) peer=%r",
                old_suppressed,
                printable(old),
            )
    _handshake_notice(
        "mllp tls handshake failed peer=%r reason=%r total=%d",
        printable(peer),
        _handshake_reason(exc),
        _handshake_failures,
    )


def log_handshake_total() -> None:
    """Report suppressed repeats and the total. Call at any listener shutdown."""
    for host, (_start, suppressed) in list(_windows.items()):
        if suppressed:
            _handshake_notice(
                "mllp tls handshake failed %d more time(s) peer=%r",
                suppressed,
                printable(host),
            )
            _windows[host] = (_clock(), 0)
    _report_dropped_lines()
    if _handshake_failures:
        log.log(NOTICE, "mllp tls handshakes failed in total: %d", _handshake_failures)


def _rdns(value: object) -> str:
    """``commonName=x, organizationName=y`` from a getpeercert name."""
    if not isinstance(value, tuple):
        return ""
    return ", ".join(f"{k}={v}" for rdn in value for k, v in rdn)


def _enc(value: object) -> str:
    """One audit-line value: control characters become ``?``, the length is
    capped, and everything but unreserved characters is percent-encoded, so a
    value holds no space, quote or ``=`` and cannot start another field."""
    return quote(printable(str(value)), safe="")


def _peer_text(peer: object) -> str:
    if isinstance(peer, tuple) and len(peer) >= 2:
        host = str(peer[0])
        text = f"[{host}]:{peer[1]}" if ":" in host else f"{host}:{peer[1]}"
        return quote(printable(text), safe=":[]")
    return _enc(peer)


def _identity(cert: object) -> dict[str, str]:
    """The identity of a verified client certificate, each value encoded for a
    log line: subject, subject alternative names, serial, issuer."""
    if not isinstance(cert, dict):
        return {}

    sans = ", ".join(f"{k}:{v}" for k, v in cert.get("subjectAltName", ()))
    return {
        "subject": _enc(_rdns(cert.get("subject"))),
        "san": _enc(sans),
        "serial": _enc(cert.get("serialNumber", "")),
        "issuer": _enc(_rdns(cert.get("issuer"))),
    }


def _fingerprint(writer: asyncio.StreamWriter) -> str:
    """SHA-256 of the client certificate as sent (DER), the stable way to name it."""
    ssl_object = writer.get_extra_info("ssl_object")
    der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    return hashlib.sha256(der).hexdigest() if der else "unknown"


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
        # A verified client always gets this line, whatever its subject. It
        # names the certificate by fingerprint, serial and issuer; the subject
        # and alternative names, which can hold a person's name, an email
        # address or a URI query, are in a second line at DEBUG only.
        fingerprint = _fingerprint(writer)
        log.log(
            NOTICE,
            "mllp client certificate peer=%s sha256=%s serial=%s issuer=%s",
            _peer_text(peer),
            fingerprint,
            ident["serial"],
            ident["issuer"],
        )
        log.debug(
            "mllp client certificate details sha256=%s subject=%s san=%s",
            fingerprint,
            ident["subject"],
            ident["san"],
        )
    return True


@dataclass(slots=True, eq=False)
class _Client:
    """One accepted connection, tracked so a shutdown can end it."""

    writer: asyncio.StreamWriter
    task: asyncio.Task[None] | None
    busy: bool = False  # a frame is in the handler
    handshaking: bool = False  # in start_tls: ended by cancelling, never by abort


_CLIENTS: weakref.WeakKeyDictionary[asyncio.Server, set[_Client]] = weakref.WeakKeyDictionary()

SHUTDOWN_GRACE_S = 5.0
MAX_PENDING_HANDSHAKES = 64
MAX_PENDING_PER_ADDRESS = 4
MAX_CONNECTIONS = 256
_refused_pending = 0
_refused_log_at: float | None = None


async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handler: Handler,
    on_junk: Callable[[bytes], Awaitable[None]],
    client: _Client | None = None,
) -> None:
    peer = writer.get_extra_info("peername")
    log.info("mllp connection opened peer=%s", peer)
    try:
        async for item in decode_frames(reader):
            if isinstance(item, JunkBytes):
                log.warning("mllp junk bytes peer=%s length=%d", peer, len(item.data))
                await on_junk(item.data)
                continue
            if client is not None:
                client.busy = True
            try:
                response = await handler(item.payload)
                writer.write(encode_mllp(response))
                await writer.drain()
            finally:
                if client is not None:
                    client.busy = False
    except (ConnectionError, ssl.SSLError, TimeoutError) as exc:
        log.warning("mllp connection broke peer=%s error=%s", peer, type(exc).__name__)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            log.warning("mllp close race peer=%s", peer)
        log.info("mllp connection closed peer=%s", peer)


def _note_refused(peer: object, what: str, count: int, limit: int) -> None:
    """One log line a window for connections refused at a TLS limit."""
    global _refused_pending, _refused_log_at
    _refused_pending += 1
    now = _clock()
    if _refused_log_at is not None and now - _refused_log_at < HANDSHAKE_LOG_WINDOW_S:
        return
    _refused_log_at = now
    log.log(
        NOTICE,
        "mllp too many %s (%d, limit %d): connection from peer=%s closed; %d refused so far",
        what,
        count,
        limit,
        _peer_text(peer),
        _refused_pending,
    )


async def serve(
    host: str,
    port: int,
    handler: Handler,
    on_junk: Callable[[bytes], Awaitable[None]],
    *,
    ssl_context: ssl.SSLContext | None = None,
    max_pending_handshakes: int = MAX_PENDING_HANDSHAKES,
    max_pending_per_address: int = MAX_PENDING_PER_ADDRESS,
    max_connections: int = MAX_CONNECTIONS,
) -> asyncio.Server:
    """Start the MLLP listener. Caller owns the returned server's lifetime and
    ends it with ``shutdown_server``. Pass ``ssl_context`` (see
    ``server_ssl_context``) for MLLP over TLS. Then at most ``max_connections``
    TLS connections may be open at once, at most ``max_pending_handshakes`` may
    be waiting to finish a handshake, and at most ``max_pending_per_address``
    of those from one address; a connection past a limit is closed and logged.
    ``host`` must name an address: an empty one would bind every interface."""
    if not host.strip():
        raise ValueError("host must not be empty")
    clients: set[_Client] = set()
    pending = 0
    pending_from: dict[str, int] = {}  # address -> handshakes; no entry at zero
    open_tls = 0

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal pending, open_tls
        client = _Client(writer, asyncio.current_task())
        clients.add(client)
        try:
            if ssl_context is None:
                await _handle_connection(reader, writer, handler, on_junk, client)
                return
            # TLS starts here, not in start_server, so a failed handshake is
            # seen with its peer address and can be logged and counted.
            peer = writer.get_extra_info("peername")
            address = _peer_host(peer)
            refused: tuple[str, int, int] | None = None
            if open_tls >= max_connections:
                refused = ("open TLS connections", open_tls, max_connections)
            elif pending >= max_pending_handshakes:
                refused = ("pending TLS handshakes", pending, max_pending_handshakes)
            elif pending_from.get(address, 0) >= max_pending_per_address:
                refused = (
                    "pending TLS handshakes from one address",
                    pending_from[address],
                    max_pending_per_address,
                )
            if refused is not None:
                _note_refused(peer, *refused)
                writer.close()
                return
            open_tls += 1  # held from admission through disconnect
            try:
                pending += 1
                pending_from[address] = pending_from.get(address, 0) + 1
                client.handshaking = True
                try:
                    started = await _start_tls(writer, ssl_context)
                finally:
                    client.handshaking = False
                    pending -= 1
                    pending_from[address] -= 1
                    if not pending_from[address]:
                        del pending_from[address]
                if started:
                    await _handle_connection(reader, writer, handler, on_junk, client)
            finally:
                open_tls -= 1
        finally:
            clients.discard(client)

    server = await asyncio.start_server(connection, host, port)
    _CLIENTS[server] = clients
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("mllp listening on %s%s", sockets, " (tls)" if ssl_context else "")
    return server


async def shutdown_server(server: asyncio.Server, grace_s: float = SHUTDOWN_GRACE_S) -> None:
    """Stop accepting, close every idle client at once, give a client with a
    frame in the handler up to ``grace_s`` seconds to finish, then cut the rest.
    Without this a connected but idle client (a normal MLLP sender keeps its
    connection open) holds the process after Ctrl+C."""
    server.close()
    clients = _CLIENTS.get(server, set())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_s
    cut = 0

    def end(client: _Client) -> None:
        # Aborting a transport inside start_tls makes it return None (an
        # AttributeError in asyncio); a client in its handshake is cancelled.
        if client.handshaking:
            if client.task is not None and not client.task.cancelling():
                client.task.cancel()
        else:
            client.writer.transport.abort()

    while clients:
        for client in list(clients):
            if not client.busy:
                end(client)
        if not any(c.busy for c in clients):
            break
        if loop.time() >= deadline:
            for client in list(clients):
                cut += 1
                end(client)
                if client.task is not None and not client.task.cancelling():
                    client.task.cancel()
            break
        await asyncio.sleep(0.05)
    tasks = {c.task for c in list(clients) if c.task is not None}
    if tasks:
        _done, left = await asyncio.wait(tasks, timeout=1.0)
        for task in left:
            task.cancel()
    if cut:
        log.warning("mllp shutdown cut %d client(s) still working after %g s", cut, grace_s)
    with contextlib.suppress(TimeoutError, OSError):
        await asyncio.wait_for(server.wait_closed(), 1.0)


@dataclass(slots=True)
class SendProgress:
    """Filled in by ``send_messages``: how many frames were handed to the
    connection. A frame counts once written, whether or not it was answered."""

    written: int = 0


NEGATIVE_ACK_CODES = frozenset({"AE", "AR", "CE", "CR"})


def check_ack(payload: bytes, ack: bytes) -> None:
    """Raise ``AckProtocolError`` unless ``ack`` answers ``payload``: an HL7
    ACK (MSH first, MSH-9 ``ACK``) with exactly one MSA, whose MSA-2 is the
    MSH-10 sent. Both are read the same way (trimmed, first component, HL7
    escapes decoded, control characters dropped), so an escaped echo matches.
    hl7bridge's listener answers ``UNKNOWN`` when it cannot read the MSH or
    MSH-10 is empty: that counts only in a negative ACK (AE, AR, CE, CR), never
    in an AA or CA, so a message without a control id is never confirmed."""
    sent = control_id_of(payload)
    if not ack.lstrip(b"\r\n").startswith(b"MSH"):
        raise AckProtocolError("the reply does not start with an MSH segment")
    try:
        message = parse_hl7(ack.decode("utf-8", errors="replace"))
    except HL7ParseError:
        raise AckProtocolError("the reply is not an HL7 message") from None
    if get_field(message, "MSH-9") != "ACK":
        raise AckProtocolError("the reply is not an ACK (MSH-9)")
    if get_segment_count(message, "MSA") != 1:
        raise AckProtocolError("the reply does not hold exactly one MSA segment")
    answered = get_field(message, "MSA-2")
    if sent and ack_value(answered) == ack_value(sent):
        return
    if answered == "UNKNOWN" and get_field(message, "MSA-1") in NEGATIVE_ACK_CODES:
        return
    raise AckProtocolError("the ACK does not answer the message sent (MSA-2 is not its MSH-10)")


def _resolve(host: str, port: int) -> asyncio.Future[list[tuple[Any, ...]]]:
    """``getaddrinfo`` on a daemon thread of its own. The default executor
    would hold ``asyncio.run`` at exit until a stuck lookup returned; this one
    is simply abandoned when the deadline passes."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[list[tuple[Any, ...]]] = loop.create_future()

    def deliver(result: list[tuple[Any, ...]], error: BaseException | None) -> None:
        if done.done():  # cancelled at the deadline
            return
        if error is not None:
            done.set_exception(error)
        else:
            done.set_result(result)

    def work() -> None:
        result: list[tuple[Any, ...]] = []
        error: BaseException | None = None
        try:
            result = list(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:  # noqa: BLE001  handed to the waiting coroutine
            error = exc
        with contextlib.suppress(RuntimeError):  # the loop has closed meanwhile
            loop.call_soon_threadsafe(deliver, result, error)

    threading.Thread(target=work, name="hl7bridge-resolve", daemon=True).start()
    return done


async def _open_connection(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Like ``asyncio.open_connection``, but the name lookup can be abandoned."""
    loop = asyncio.get_running_loop()
    last: OSError = OSError(f"no address found for port {port}")
    for family, kind, proto, _name, address in await _resolve(host, port):
        sock = socket.socket(family, kind, proto)
        try:
            sock.setblocking(False)
            await loop.sock_connect(sock, address)
        except OSError as exc:
            sock.close()
            last = exc
            continue
        except BaseException:
            sock.close()
            raise
        return await asyncio.open_connection(sock=sock)
    raise last


async def send_messages(
    host: str,
    port: int,
    payloads: Iterable[bytes],
    *,
    ack_timeout_s: float = 10.0,
    acks: list[bytes] | None = None,
    interval_s: float = 0.0,
    ssl_context: ssl.SSLContext | None = None,
    progress: SendProgress | None = None,
) -> list[bytes]:
    """Send each payload as one MLLP frame and collect the ACK responses.

    A missing or late ACK raises TimeoutError; silence is never success. An
    ACK that is cut short, is not one ACK, or answers another message (see
    ``check_ack``) raises ``AckProtocolError``; it is never collected.
    Pass ``acks`` to keep the ACKs that did arrive when either happens, and
    ``progress`` to learn how many frames were written.
    ``ack_timeout_s`` is one deadline for each phase: connecting (name lookup,
    TCP and the TLS handshake together), and then, for each message, writing
    it and receiving its ACK together. ``interval_s`` is the pause between one
    ACK and the next send.
    Pass ``ssl_context`` (see ``client_ssl_context``) for MLLP over TLS. The
    server's certificate must be valid for ``host``, so a context that does
    not verify the chain and the host name is refused (TlsConfigError). Only
    that check is made on a context you build yourself: the TLS 1.2 floor, the
    alternative-names-only host check and renegotiation off are guarantees of
    ``client_ssl_context``, not of this function.
    """
    if not host.strip():
        raise ValueError("host must not be empty")
    if ssl_context is not None and (
        not ssl_context.check_hostname or ssl_context.verify_mode != ssl.CERT_REQUIRED
    ):
        raise TlsConfigError(
            "the TLS context must verify the server: check_hostname on and"
            " verify_mode CERT_REQUIRED (see client_ssl_context)."
        )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ack_timeout_s
    try:
        reader, writer = await asyncio.wait_for(_open_connection(host, port), timeout=ack_timeout_s)
    except OSError as exc:  # refused, no route, unknown host, timed out, several of those
        raise ConnectError(exc) from exc
    if ssl_context is not None:
        # The TCP connection is made; from here a failure is a TLS failure.
        remaining = deadline - loop.time()
        try:
            if remaining <= 0:
                raise TimeoutError("no time left for the TLS handshake")
            await writer.start_tls(
                ssl_context,
                server_hostname=host,
                ssl_handshake_timeout=remaining,
                ssl_shutdown_timeout=min(ack_timeout_s, 5.0),
            )
        except (ssl.SSLError, ConnectionError, TimeoutError) as exc:
            writer.transport.abort()
            # Nothing has been written yet: the connection never got going.
            # asyncio reports its own handshake timeout as ConnectionAbortedError,
            # and so does Windows for a reset: only one at the deadline is a stall.
            if isinstance(exc, ConnectionAbortedError) and loop.time() >= deadline - 0.05:
                raise TlsHandshakeError(TimeoutError("no TLS handshake in time")) from exc
            raise TlsHandshakeError(exc) from exc
    if acks is None:
        acks = []
    clean = False
    try:
        for i, payload in enumerate(payloads):
            if i and interval_s > 0:
                await asyncio.sleep(interval_s)
            ack = await asyncio.wait_for(
                _exchange(reader, writer, payload, progress), timeout=ack_timeout_s
            )
            check_ack(payload, ack)
            acks.append(ack)
        clean = True
    finally:
        if clean:
            writer.close()
            with contextlib.suppress(OSError):  # a slow TLS shutdown must not mask the result
                await writer.wait_closed()
        else:
            # A peer that stopped reading must not hold a close that waits to flush.
            writer.transport.abort()
    return acks


async def _exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    payload: bytes,
    progress: SendProgress | None,
) -> bytes:
    writer.write(encode_mllp(payload))
    if progress is not None:
        progress.written += 1
    await writer.drain()
    return await _read_one_frame(reader)


async def _read_one_frame(reader: asyncio.StreamReader) -> bytes:
    async for item in decode_frames(reader):
        if isinstance(item, Frame):
            return item.payload
        raise AckProtocolError("the ACK was cut short or is not an MLLP frame")
    raise ConnectionError("connection closed before ACK frame arrived")
