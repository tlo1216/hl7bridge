"""Exception hierarchy. Every failure path raises one of these; nothing is swallowed."""


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
    """The FHIR server rejected a request. Carries status and body."""

    def __init__(self, status_code: int, url: str, body: str) -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"FHIR server returned {status_code} for {url}: {body[:500]}")
