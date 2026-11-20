"""HL7 v2 messages to FHIR R4B resources: pure functions, no I/O.

``message_to_bundle`` maps one message to a transaction Bundle. The segment
mappers (``pid_to_patient``, ``pv1_to_encounter``,
``orc_obr_to_service_request``, ``obr_to_diagnostic_report``,
``obx_to_observation``) and the code tables are what the store path's
row mappers share. ``spec.py`` lists every field each mapper reads, and
``docs/mapping.md`` is generated from it.
"""

from .bundle import SUPPORTED, MappedMessage, bundle_json, message_to_bundle
from .jsontext import dumps as json_text

__all__ = ["SUPPORTED", "MappedMessage", "bundle_json", "json_text", "message_to_bundle"]
