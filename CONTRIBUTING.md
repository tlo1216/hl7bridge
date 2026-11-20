# Contributing

Thanks for your interest. Small, focused pull requests are easiest to
review. For anything large, open an issue first so we can agree on the
approach.

## Ground rules

- **Synthetic data only.** Never commit, paste or attach real patient data,
  not in code, tests, fixtures, issues or screenshots. Use
  `hl7bridge synth` or a hand-made fake with names like `ZZZTEST ALPHA`.
- **No PHI in logs or error text.** Log store row ids, hashes, counts and
  reason codes, never field values. `tests/unit/test_phi_logging.py` must
  keep passing; if you add a new log or error path, add it to that test.
- **Fail loudly.** No blind `except`, no silent drops. A degraded path
  must never look healthy. See [docs/principles.md](docs/principles.md).

## Setup

With [uv](https://docs.astral.sh/uv/):

```
uv sync
```

With pip (any OS, Python 3.12 or newer):

```
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS or Linux:     source .venv/bin/activate
python -m pip install -e . pytest ruff mypy types-pyyaml
```

## The checks

CI runs these on Ubuntu, Windows and macOS. Run them before you push
(prefix each with `uv run` if you use uv):

```
ruff format --check .
ruff check .
mypy
pytest -m "not fhir"
python tools/check_references.py
```

The FHIR system tests need a HAPI server. Start one with
`docker run -p 127.0.0.1:8080:8080 hapiproject/hapi:v8.12.0-2`, set
`HL7BRIDGE_FHIR_BASE=http://127.0.0.1:8080/fhir`, then run `pytest -m fhir`.

## Adding a lint rule

1. Add the id, severity and message to `KNOWN_RULES` in
   `src/hl7bridge/lint/rules.py`, and the check to `lint/runner.py`.
2. Add two tests to `tests/unit/test_lint.py`: one where the rule fires on a
   bad message and one where it stays silent on a good one.
3. Run `tests/system/test_lint_corpus.py`. Findings over the generated corpus
   must still equal the seeded defects exactly. If your rule finds something
   real in the corpus, seed it in the generator and the manifest instead of
   loosening the test.

## Adding or changing a FHIR mapping

1. Change the mapper in `src/hl7bridge/fhir/mapper.py`.
2. Add a unit test in `tests/unit/test_fhir_mapper.py` with the exact
   expected resource.
3. Record any deviation from the HL7 v2-to-FHIR IG in
   `docs/adr/0003-fhir-mapping-approach.md`.

## Pull requests

- One topic per PR, with tests.
- Update `CHANGELOG.md` under "Unreleased".
- Keep docs in plain language.

## License

By contributing you agree that your contribution is licensed under the
Apache License 2.0, the same license as the project (inbound equals
outbound). No separate agreement is needed.
