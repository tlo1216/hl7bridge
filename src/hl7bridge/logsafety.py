"""Logging setup that keeps patient identifiers out of log output.

Two layers:

1. ``install_third_party_redaction`` puts a filter on the ``httpx`` and
   ``httpcore`` loggers that strips the query string from every URL in
   their records. FHIR conditional updates carry the MRN or accession in
   the query (``?identifier=system|value``), and httpx logs every request
   URL at INFO. The FHIR client installs this on construction, so a library
   user who turns on DEBUG logging is covered too.
2. ``configure_logging`` is the CLI setup: one stderr handler on the root
   logger with the same URL filter, the root at INFO, and httpx and httpcore
   at WARNING so request lines are not emitted at all.

Log records from hl7bridge itself carry store row ids, short hashes,
hospital ids, message types from configuration, counts and reason codes.
They never carry sender-written field values. ``tests/unit/test_phi_logging.py``
enforces this with canary values on every path.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from typing import TextIO

THIRD_PARTY_HTTP_LOGGERS = ("httpx", "httpcore")

# The lookahead skips a query that is already redacted, so a record that
# passes two filters (logger and handler) is not mangled.
_URL_QUERY_RE = re.compile(r"""(https?://[^\s?#'"<>]+)\?(?!\[query redacted\])[^\s#'"<>]*""")
_REDACTED_QUERY = "?[query redacted]"
_HANDLER_MARK = "_hl7bridge_handler"
_PLAIN = logging.Formatter()


def redact_url_queries(text: str) -> str:
    """Replace the query string of every http(s) URL in ``text``."""
    return _URL_QUERY_RE.sub(lambda m: m.group(1) + _REDACTED_QUERY, text)


def short_hash(value: str) -> str:
    """First 12 hex digits of SHA-256. Lets a log line be matched to a
    stored row without printing a sender-written value such as MSH-10."""
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


class UrlQueryRedactor(logging.Filter):
    """Rewrites each record so no URL in it keeps its query string.

    The message is formatted once and stored back with no args, so the
    redaction holds whatever handler or formatter runs later.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # A malformed format string: keep the template only, never the args.
            message = str(record.msg)
        redacted = redact_url_queries(message)
        if redacted != message or record.args:
            record.msg = redacted
            record.args = None
        if record.exc_info and not record.exc_text:
            # Pre-render the traceback so URLs in chained exception text are
            # redacted too; Formatter.format reuses exc_text when it is set.
            record.exc_text = redact_url_queries(_PLAIN.formatException(record.exc_info))
        return True


def _has_redactor(target: logging.Filterer) -> bool:
    return any(isinstance(f, UrlQueryRedactor) for f in target.filters)


def install_third_party_redaction() -> None:
    """Idempotent: add the URL filter to the httpx and httpcore loggers."""
    for name in THIRD_PARTY_HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if not _has_redactor(logger):
            logger.addFilter(UrlQueryRedactor())


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
    handler.addFilter(UrlQueryRedactor())
    root.addHandler(handler)
    root.setLevel(level)
    for name in THIRD_PARTY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
