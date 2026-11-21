"""Exception hierarchy. Every failure path raises one of these; nothing is swallowed."""

import json
from urllib.parse import urlsplit

# The FHIR R4 / R4B IssueType value set
# (http://hl7.org/fhir/R4B/valueset-issue-type.html). A server can put any
# text in issue.code; only these exact codes are ever shown.
FHIR_ISSUE_TYPES = frozenset(
    {
        "invalid",
        "structure",
        "required",
        "value",
        "invariant",
        "security",
        "login",
        "unknown",
        "expired",
        "forbidden",
        "suppressed",
        "processing",
        "not-supported",
        "duplicate",
        "multiple-matches",
        "not-found",
        "deleted",
        "too-long",
        "code-invalid",
        "extension",
        "too-costly",
        "business-rule",
        "conflict",
        "transient",
        "lock-error",
        "no-store",
        "exception",
        "timeout",
        "incomplete",
        "throttled",
        "informational",
    }
)
UNRECOGNIZED_ISSUE_CODE = "unrecognized-code"


def _url_path(url: str) -> str:
    """The path of ``url`` only: no scheme, host, user info, query or
    fragment. A mistyped URL can carry its user info in the path
    (``http:/alice:pw@host``), so a path holding ``@`` is never shown."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[unparseable URL]"
    if "@" in parts.path or (parts.scheme and not parts.netloc):
        return "[unparseable URL]"
    return parts.path or "/"


# Exit codes, documented once in docs/cli.md.
EXIT_OK = 0
EXIT_FINDINGS = 1  # findings, rejected messages, a failed check
EXIT_USAGE = 2  # bad arguments, bad profiles, unusable files or folders
EXIT_CONNECTION = 3  # a listener or server did not answer
EXIT_INTERNAL = 4  # a bug in hl7bridge
EXIT_INTERRUPTED = 130  # Ctrl+C


class BridgeError(Exception):
    """Base class for all hl7bridge errors.

    ``quotes_input`` is True for the classes whose message can quote a
    sender-written value (a timestamp, a broken segment). The CLI prints the
    message of every other class, and only the class name for these.

    ``hint`` is fixed text that tells the user what to try next. The CLI
    prints it after the message. ``exit_code`` is the CLI exit status.
    """

    quotes_input: bool = False
    exit_code: int = EXIT_FINDINGS

    def __init__(self, *args: object, hint: str = "") -> None:
        super().__init__(*args)
        self.hint = hint


class ConfigError(BridgeError):
    """Hospital or application configuration is invalid."""

    exit_code = EXIT_USAGE


class UsageError(BridgeError):
    """A command was given arguments or paths it cannot use."""

    exit_code = EXIT_USAGE


class ListenError(BridgeError):
    """The MLLP listener could not bind its address."""

    exit_code = EXIT_USAGE


class TlsConfigError(BridgeError):
    """A TLS certificate, key or CA file is missing or unusable."""

    exit_code = EXIT_USAGE


class NetworkError(BridgeError):
    """A listener or server did not answer, or the connection broke."""

    exit_code = EXIT_CONNECTION


class ConnectError(NetworkError):
    """The sender never connected to the listener: nothing was written.
    ``cause`` is the OS error (refused, no route, unknown host, timed out)."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class TlsHandshakeError(NetworkError):
    """The TCP connection was made but the TLS handshake failed before any
    frame was written. ``cause`` is the ssl or timeout error."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class StoreError(BridgeError):
    """The database file is not usable: wrong schema version, not SQLite."""

    exit_code = EXIT_USAGE


class DemoDirError(BridgeError):
    """The demo refused to use or delete a folder it cannot prove it owns."""

    exit_code = EXIT_USAGE


class HL7ParseError(BridgeError):
    """Payload could not be parsed as an HL7 v2 message at all."""

    quotes_input = True


class UnknownHospitalError(BridgeError):
    """MSH-4 / MSH-6 did not match any configured hospital."""


class MessageValidationError(BridgeError):
    """Message parsed but failed validation for its type.

    ``reason`` is a stable machine-readable code (e.g. ``missing_accession``)
    used in the quarantine table, the ACK and the log. ``field`` names the
    HL7 field that failed (``PID-7``); it is fixed text from the code, never
    from the message, so it may go back to the sender in the ACK.
    ``detail`` is human context that can quote the rejected value, so it is
    written to the quarantine row only.
    """

    quotes_input = True

    def __init__(self, reason: str, detail: str = "", *, field: str = "") -> None:
        self.reason = reason
        self.detail = detail
        self.field = field
        super().__init__(f"{reason}: {detail}" if detail else reason)


class TimestampError(BridgeError):
    """HL7 timestamp missing, malformed, or of insufficient precision."""

    quotes_input = True


class FhirMappingError(BridgeError):
    """A stored row could not be mapped to a valid FHIR resource."""


class MappingError(BridgeError):
    """A message could not be mapped to FHIR (``hl7bridge.v2fhir``).

    The text is built only from a field position (``OBX-11``), the segment
    number, and a fixed reason phrase from the code. There is no argument
    for the value, so the message cannot quote one; the CLI prints it.
    """

    def __init__(self, field: str, reason: str, *, segment: int = 1) -> None:
        self.field = field
        self.reason = reason
        self.segment = segment
        where = field if segment == 1 else f"{field} (segment {segment})"
        super().__init__(f"{where}: {reason}")


class FhirPostError(BridgeError):
    """The FHIR server rejected a request, or its answer was unusable.

    The message holds only the status code, the URL path with no query and
    a fixed reason phrase. Conditional-update URLs carry MRNs and accessions
    in the query, and server bodies often echo the request, so neither is
    ever part of the message. The full URL and body stay on the object for
    tests and for callers that know what they are doing; never log them.
    """

    def __init__(self, status_code: int, url: str, body: str = "", *, reason: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        self.reason = reason
        self.path = _url_path(url)
        # The httpx transport error class when the request never got an
        # answer (ConnectError, ReadTimeout, ReadError...), else "".
        self.transport = ""
        if status_code:
            text = f"FHIR server returned {status_code} for {self.path}"
        else:
            text = f"FHIR request to {self.path} failed"
        codes = self.issue_codes
        if codes:
            text += f" (OperationOutcome issue codes: {', '.join(codes)})"
        if reason:
            text += f": {reason}"
        super().__init__(text)

    @property
    def exit_code(self) -> int:  # type: ignore[override]
        """3 when the server never answered, 1 when it answered with an error."""
        return EXIT_CONNECTION if self.status_code == 0 else EXIT_FINDINGS

    @property
    def issue_codes(self) -> list[str]:
        """OperationOutcome ``issue.code`` values from the body, if any.

        A code from the FHIR IssueType value set is returned as is. Any other
        value becomes the fixed text ``unrecognized-code``, so a body cannot
        smuggle free text (a lower-cased surname, say) through here. A body
        of the wrong shape (``issue`` not a list, entries not objects)
        yields no codes instead of an exception.
        """
        try:
            payload = json.loads(self.body)
        except (ValueError, TypeError):
            return []
        if not isinstance(payload, dict) or payload.get("resourceType") != "OperationOutcome":
            return []
        issues = payload.get("issue")
        if not isinstance(issues, list):
            return []
        codes: list[str] = []
        for issue in issues:
            if not isinstance(issue, dict) or "code" not in issue:
                continue
            raw = issue["code"]
            code = raw if isinstance(raw, str) and raw in FHIR_ISSUE_TYPES else None
            shown = code or UNRECOGNIZED_ISSUE_CODE
            if shown not in codes:
                codes.append(shown)
        return codes
