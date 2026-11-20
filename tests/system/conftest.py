"""When a live-server test fails on an error answer, show what HAPI said.

``FhirPostError`` keeps the server body off its message on purpose (a body
can echo patient data). The system tests post synthetic data only, so here,
and only here, a failing test report gets each OperationOutcome issue's
severity, code, diagnostics and details text.
"""

from __future__ import annotations

import json

import pytest

from hl7bridge.errors import FhirPostError

LIMIT = 2000


def _chain(exc: BaseException | None):
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def operation_outcome_text(exc: FhirPostError) -> str:
    """The issues of an OperationOutcome body, or a note when there are none."""
    head = f"HAPI answered {exc.status_code} for {exc.path}"
    try:
        payload = json.loads(exc.body)
    except (ValueError, TypeError):
        return f"{head}; the body is not JSON ({len(exc.body)} chars)"
    if not isinstance(payload, dict) or payload.get("resourceType") != "OperationOutcome":
        return f"{head}; the body is not an OperationOutcome"
    lines = [head]
    for issue in payload.get("issue") or []:
        if not isinstance(issue, dict):
            continue
        details = issue.get("details")
        text = details.get("text", "") if isinstance(details, dict) else ""
        lines.append(
            f"  {issue.get('severity', '?')} {issue.get('code', '?')}:"
            f" {issue.get('diagnostics', '')} {text}".rstrip()
        )
    return "\n".join(lines)[:LIMIT]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if call.excinfo is None or report.passed:
        return
    for exc in _chain(call.excinfo.value):
        if isinstance(exc, FhirPostError) and exc.body:
            report.sections.append(("HAPI OperationOutcome", operation_outcome_text(exc)))
            break
