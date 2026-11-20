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
    """The path of ``url`` only: no scheme, host, user info, query or fragment."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return "[unparseable URL]"
    return path or "/"


class BridgeError(Exception):
    """Base class for all hl7bridge errors.

    ``quotes_input`` is True for the classes whose message can quote a
    sender-written value (a timestamp, a broken segment). The CLI prints the
    message of every other class, and only the class name for these.
    """

    quotes_input: bool = False


class ConfigError(BridgeError):
    """Hospital or application configuration is invalid."""


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
