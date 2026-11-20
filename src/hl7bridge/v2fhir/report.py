"""OBR to DiagnosticReport (IG page ConceptMap-segment-obr-to-diagnosticreport).

The report is keyed on the accession (type ACSN) like the store path.
Every OBX becomes an Observation in ``result``. For an imaging report the
text lines also stay together: joined in ``conclusion`` as before, and as
a plain-text ``presentedForm`` attachment.
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
from .order import accession_identifier, procedure_code
from .tables import REPORT_STATUS

TEXT_PLAIN = "text/plain; charset=utf-8"


def presented_form(text: str) -> Attachment:
    # The model holds base64Binary as the decoded bytes and encodes them
    # again when it is serialized.
    return Attachment.model_validate(
        {"contentType": TEXT_PLAIN, "data": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    )


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
    issued = timestamp(ctx, "OBR-22")
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
