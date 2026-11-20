## What and why

## Checklist

- [ ] Tests added or updated, and `pytest -m "not fhir"` passes
- [ ] `ruff format --check .`, `ruff check .` and `mypy` pass
- [ ] `python tools/check_references.py` passes
- [ ] Docs and `CHANGELOG.md` (under Unreleased) updated
- [ ] No real patient data anywhere: code, tests, fixtures, screenshots
- [ ] New log or error paths are covered by `tests/unit/test_phi_logging.py`
