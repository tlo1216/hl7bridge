"""Logging setup that keeps patient identifiers out of log output.

The filter here, ``LogRedactor``, does two things to every record it sees:

1. A record from an HTTP client logger (``httpx``, ``httpcore`` or any
   logger below them, such as ``httpcore.http11``) is rebuilt from an
   allowlist of safe fields: the request method, the URL with no user info
   and no query, the HTTP version, the status code, and httpcore's trace
   event name. Everything else the library put in the record (the reason
   phrase, response headers such as ``Location``, exception text) is
   dropped, because a server can put an identifier in any of it.
2. Any other record keeps its text, but every URL in it loses its query
   string and user info, and a traceback attached to it is rendered with
   the same redaction.

FHIR conditional updates carry the MRN or accession in the query
(``?identifier=system|value``), and httpx logs every request URL at INFO.

Where the filter is installed:

- ``install_third_party_redaction`` puts it on ``httpx``, ``httpcore`` and
  every logger below them that exists or that httpcore is known to use. A
  logger filter only runs for records created on that exact logger, so the
  child loggers need their own copy. The FHIR client calls this on
  construction, so a library user with their own handlers is covered.
- ``configure_logging`` (the CLI setup) also puts it on the one stderr
  handler it installs. A handler filter runs for every record the handler
  emits, from any logger, so this is the layer that cannot be bypassed by a
  logger name nobody listed. It also holds the HTTP client loggers and
  ``pydicom`` at WARNING.

Log records from hl7bridge itself carry store row ids, hospital ids,
message types from configuration, counts and reason codes. They never
carry sender-written field values. ``tests/unit/test_phi_logging.py``
enforces this with canary values on every path.
"""

from __future__ import annotations

import logging
import re
import sys
import unicodedata
from typing import TextIO
from urllib.parse import urlsplit

THIRD_PARTY_HTTP_LOGGERS = ("httpx", "httpcore")
# The loggers httpcore 1.x creates at import. Listing them means the filter
# is on them even before httpcore is imported.
KNOWN_HTTP_CHILD_LOGGERS = (
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
QUIET_LOGGERS = (*THIRD_PARTY_HTTP_LOGGERS, "pydicom")

# A URL query: after an absolute http(s) URL, or after a relative target
# that starts with "/" (``/fhir/Patient?identifier=...``), as servers send
# in Location headers and some libraries log. The lookahead skips a query
# that is already redacted, so a record that passes two filters (logger and
# handler) is not mangled.
_URL_QUERY_RE = re.compile(
    r"""((?:https?://|(?<![\w/:])/)[^\s?#'"<>]*)\?(?!\[query redacted\])[^\s#'"<>]*"""
)
_URL_USERINFO_RE = re.compile(r"""(https?://)[^\s/?#'"<>@]+@""")
_REDACTED_QUERY = "?[query redacted]"
_REDACTED_USERINFO = "[user-info-redacted]@"
_HANDLER_MARK = "_hl7bridge_handler"
_DONE_MARK = "_hl7bridge_redacted"
_PLAIN = logging.Formatter()

_HTTPX_REQUEST_TEMPLATE = 'HTTP Request: %s %s "%s %d %s"'
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_HTTP_VERSIONS = frozenset({"HTTP/0.9", "HTTP/1.0", "HTTP/1.1", "HTTP/2", "HTTP/3"})
_TRACE_EVENT_RE = re.compile(r"[a-z][a-z0-9_]{0,40}\.(?:started|complete|failed)")
_TRACE_EXC_RE = re.compile(r"exception=([A-Za-z_][A-Za-z0-9_]{0,60})\(")
_TRACE_STATUS_RE = re.compile(r"return_value=\(b'(HTTP/[0-9.]{1,3})', ([1-5][0-9]{2}),")


def redact_url_queries(text: str) -> str:
    """Remove the query string and user info from every URL in ``text``."""
    text = _URL_QUERY_RE.sub(lambda m: m.group(1) + _REDACTED_QUERY, text)
    return _URL_USERINFO_RE.sub(lambda m: m.group(1) + _REDACTED_USERINFO, text)


UNPARSEABLE_URL = "[unparseable URL]"


def safe_url(url: object) -> str:
    """``scheme://host[:port]/path`` of ``url``, with no user info, query or
    fragment. Use it wherever a configured or requested URL is printed.

    It never returns user info, even for a mistyped URL: with no scheme or
    one slash too few (``alice:pw@host/fhir``, ``http:/alice:pw@host``)
    ``urlsplit`` puts the password in the path, so any path holding ``@``
    gives ``[unparseable URL]``."""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        return UNPARSEABLE_URL
    if "@" in parts.path:
        return UNPARSEABLE_URL
    if ":" in host:
        host = f"[{host}]"
    query = _REDACTED_QUERY if parts.query else ""
    if parts.scheme and host:
        return f"{parts.scheme}://{host}{port}{parts.path}{query}"
    if parts.scheme or parts.netloc:
        # A scheme with no host, or a host with no scheme: not a URL we can
        # show in part without risking the user info.
        return UNPARSEABLE_URL
    return f"{parts.path or '/'}{query}"


def printable(text: object, limit: int = 160) -> str:
    """Text from outside (an ACK, a server answer) made safe for a terminal.

    Every control or format character (C0, C1, DEL, bidi overrides and the
    like) becomes ``?``, so a remote party cannot move the cursor, set the
    window title or hide text, and the length is capped."""
    cleaned = "".join("?" if unicodedata.category(ch).startswith("C") else ch for ch in str(text))
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."


def _is_http_logger(name: str) -> bool:
    return any(name == root or name.startswith(root + ".") for root in THIRD_PARTY_HTTP_LOGGERS)


def safe_http_message(record: logging.LogRecord) -> str:
    """Rebuild an HTTP client record from the allowlisted fields only."""
    args = record.args
    if (
        record.name == "httpx"
        and record.msg == _HTTPX_REQUEST_TEMPLATE
        and isinstance(args, tuple)
        and len(args) == 5
    ):
        method, url, version, status, _reason_phrase = args
        method_text = str(method).upper()
        if method_text not in _HTTP_METHODS:
            method_text = "[method]"
        version_text = str(version) if str(version) in _HTTP_VERSIONS else "HTTP/?"
        status_text = str(status) if isinstance(status, int) else "?"
        return f'HTTP Request: {method_text} {safe_url(url)} "{version_text} {status_text}"'
    if record.name.startswith("httpcore"):
        text = str(record.msg)
        event = text.split(" ", 1)[0]
        if _TRACE_EVENT_RE.fullmatch(event):
            if event.endswith(".failed"):
                exc = _TRACE_EXC_RE.search(text)
                return f"{event} exception={exc.group(1) if exc else '?'}"
            status = _TRACE_STATUS_RE.search(text)
            if event == "receive_response_headers.complete" and status:
                return f"{event} {status.group(1)} {status.group(2)}"
            return event
    return f"{record.name} record withheld (not on the safe field list)"


class LogRedactor(logging.Filter):
    """Rewrites each record so it holds no URL query, no URL user info and,
    for HTTP client loggers, nothing outside the safe field list.

    The message is formatted once and stored back with no args, so the
    redaction holds whatever handler or formatter runs later. A record is
    rewritten once; a second filter on the same path leaves it alone.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _DONE_MARK, False):
            return True
        if _is_http_logger(record.name):
            record.msg = safe_http_message(record)
            record.args = None
            if record.exc_info:
                exc_type = record.exc_info[0]
                name = exc_type.__name__ if exc_type is not None else "?"
                record.msg = f"{record.msg} (exception {name}; text withheld)"
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        else:
            try:
                message = record.getMessage()
            except (TypeError, ValueError):
                # A malformed format string: keep the template only, never the args.
                message = str(record.msg)
            redacted = redact_url_queries(message)
            if redacted != message or record.args:
                record.msg = redacted
                record.args = None
            # Render the traceback here, redacted, even when another handler
            # already cached an unredacted copy in exc_text: Formatter.format
            # reuses exc_text whenever it is set.
            if record.exc_info:
                record.exc_text = redact_url_queries(_PLAIN.formatException(record.exc_info))
            elif record.exc_text:
                record.exc_text = redact_url_queries(record.exc_text)
            if record.stack_info:
                record.stack_info = redact_url_queries(record.stack_info)
        setattr(record, _DONE_MARK, True)
        return True


# The earlier name, kept for callers that import it.
UrlQueryRedactor = LogRedactor


def _has_redactor(target: logging.Filterer) -> bool:
    return any(isinstance(f, LogRedactor) for f in target.filters)


def http_logger_names() -> list[str]:
    """``httpx``, ``httpcore``, httpcore's known children, and every other
    existing logger below either root."""
    names = {*THIRD_PARTY_HTTP_LOGGERS, *KNOWN_HTTP_CHILD_LOGGERS}
    for name, obj in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(obj, logging.Logger) and _is_http_logger(name):
            names.add(name)
    return sorted(names)


def install_third_party_redaction() -> None:
    """Idempotent: add the filter to every HTTP client logger."""
    for name in http_logger_names():
        logger = logging.getLogger(name)
        if not _has_redactor(logger):
            logger.addFilter(LogRedactor())


def configure_logging(level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """CLI logging setup. Safe to call more than once."""
    install_third_party_redaction()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    setattr(handler, _HANDLER_MARK, True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(LogRedactor())
    root.addHandler(handler)
    root.setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
