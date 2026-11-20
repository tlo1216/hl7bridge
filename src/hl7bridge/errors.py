"""Exception hierarchy. Every failure path raises one of these; nothing is swallowed."""

import json
import re
from urllib.parse import urlsplit

_ISSUE_CODE_RE = re.compile(r"[a-z][a-z-]{0,39}")


class BridgeError(Exception):
    """Base class for all hl7bridge errors."""


class ConfigError(BridgeError):
    """Hospital or application configuration is invalid."""


class HL7ParseError(BridgeError):
    """Payload could not be parsed as an HL7 v2 message at all."""


class UnknownHospitalError(BridgeError):
    """MSH-4 / MSH-6 did not match any configured hospital."""


class MessageValidationError(BridgeError):
    """Message parsed but failed validation for its type.

    ``reason`` is a stable machine-readable code (e.g. ``missing_accession``)
    used in the quarantine table and in tests; ``detail`` is human context.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class TimestampError(BridgeError):
    """HL7 timestamp missing, malformed, or of insufficient precision."""


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
        self.path = urlsplit(url).path or url.split("?", 1)[0]
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

        Only values shaped like FHIR codes (lower case letters and hyphens)
        are returned, so a body cannot smuggle free text through here.
        """
        try:
            payload = json.loads(self.body)
        except (ValueError, TypeError):
            return []
        if not isinstance(payload, dict) or payload.get("resourceType") != "OperationOutcome":
            return []
        codes: list[str] = []
        for issue in payload.get("issue") or []:
            code = issue.get("code") if isinstance(issue, dict) else None
            if isinstance(code, str) and _ISSUE_CODE_RE.fullmatch(code) and code not in codes:
                codes.append(code)
        return codes
