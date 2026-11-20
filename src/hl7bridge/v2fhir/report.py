"""OBR to DiagnosticReport (IG page ConceptMap-segment-obr-to-diagnosticreport).

The report is keyed on the accession (type ACSN) like the store path.
Each structured result (an OBX that is not narrative) is an Observation in
``result``. The narrative, the OBX lines typed TX or FT, is the report's
text: joined in ``conclusion`` and as a plain-text ``presentedForm``
attachment, and never an Observation per line. A corrected report that no
longer carries a result retires it when posted (``fhir/post.py``).
"""

from __future__ import annotations

import base64
from typing import Any

from fhir.resources.R4B.attachment import Attachment
from fhir.resources.R4B.codeableconcept import CodeableConcept
from fhir.resources.R4B.diagnosticreport import DiagnosticReport
from fhir.resources.R4B.identifier import Identifier
from fhir.resources.R4B.reference import Reference

from ..errors import MappingError
from .common import MapContext, timestamp
from .observation import NARRATIVE_TYPES
from .order import accession_identifier, procedure_code
from .tables import REPORT_STATUS

TEXT_PLAIN = "text/plain; charset=utf-8"


def presented_form(text: str) -> Attachment:
    # The model holds base64Binary as the decoded bytes and encodes them
    # again when it is serialized.
    return Attachment.model_validate(
        {"contentType": TEXT_PLAIN, "data": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    )


def is_narrative(ctx: MapContext, seg: int) -> bool:
    """True for an OBX that is part of the report text (OBX-2 TX or FT)."""
    return ctx.reader.get("OBX-2", seg=seg) in NARRATIVE_TYPES


def report_text(ctx: MapContext) -> str:
    """The narrative lines of the report, one per OBX-5 repetition, each
    repetition read whole, empty lines left out."""
    reader = ctx.reader
    lines: list[str] = []
    for seg in range(1, reader.count("OBX") + 1):
        if is_narrative(ctx, seg):
            for rep in range(1, reader.repetitions("OBX-5", seg=seg) + 1):
                line = reader.text("OBX-5", rep=rep, seg=seg)
                if line:
                    lines.append(line)
    return "\n".join(lines)


def obr_to_diagnostic_report(
    ctx: MapContext,
    patient_ref: str,
    *,
    result_refs: list[str],
    report_text: str,
) -> DiagnosticReport:
    r = ctx.reader
    status_code = r.get("OBR-25")
    if not status_code:
        raise MappingError("OBR-25", "no result status; DiagnosticReport.status is required")
    status = REPORT_STATUS.lookup(status_code, "OBR-25")
    identifier = accession_identifier(ctx)
    kwargs: dict[str, Any] = {}
    effective = timestamp(ctx, "OBR-7")
    if effective:
        kwargs["effectiveDateTime"] = effective
    issued = timestamp(ctx, "OBR-22", instant=True)
    if issued:
        kwargs["issued"] = issued
    if result_refs:
        kwargs["result"] = [Reference(reference=ref) for ref in result_refs]
    if report_text:
        kwargs["conclusion"] = report_text
        kwargs["presentedForm"] = [presented_form(report_text)]
    # The order is linked by its accession identifier, not written: an ORU
    # does not change the ServiceRequest, and a logical reference needs no
    # lookup on the server.
    kwargs["basedOn"] = [
        Reference(
            type="ServiceRequest",
            identifier=Identifier(
                type=identifier.type, system=identifier.system, value=identifier.value
            ),
        )
    ]
    return DiagnosticReport(
        identifier=[identifier],
        status=status,
        code=procedure_code(ctx) or CodeableConcept(text="Imaging report"),
        subject=Reference(reference=patient_ref),
        **kwargs,
    )
