# HL7 v2 to FHIR conformance evidence

This is a reproducible comparison of selected elements against the
HL7 v2-to-FHIR Implementation Guide **1.0.0 (STU 1)**, based on FHIR R4
4.0.1. It demonstrates both matches and gaps, not complete IG conformance.
hl7bridge constructs R4B models and emits transaction Bundles for R4 servers.
No server is contacted and no patient data is used.

## Sources and sample provenance

Reviewed on 2026-11-21:

- The permanent [1.0.0 test conversions page](https://hl7.org/fhir/uv/v2mappings/STU1/test_conversions.html)
  publishes A01, ORM O01 and ORU R01 examples. Each corresponding FHIR Bundle
  is still marked "To be provided". These are informative examples, not a
  supplied set of executable expected Bundles.
- The [message map index](https://hl7.org/fhir/uv/v2mappings/STU1/message_maps.html)
  lists ADT_A01, ORM_O01 and ORU_R01, but no separate A04, A08 or A40 map.
- Direct HTTPS download from this workspace failed with a connection refusal
  (WinError 10061). The pages were readable through web search. Per the
  requested fallback, **all bundled messages here are authored synthetic
  equivalents**, not downloaded IG samples and not byte-for-byte replacements.
  Their names begin ZZZTEST and contact details use example.invalid.
- A01, ORM and ORU exercise the same core PID/PV1, ORC/OBR and OBX concepts
  as the published examples, with reduced segment coverage. A04, A08 and A40
  are additional synthetic events. The separate ORU SN case exercises a
  structured-number result like the published ORU, using valid SN components.
  It is retained even when the entire message is rejected.

The expected values in
[`expectations.json`](../tests/fixtures/conformance/expectations.json)
were hand-authored from the cited IG maps and the synthetic inputs. They are
independent of `v2fhir/spec.py`, mapper code and mapper output. Each table row
links its map. The [mapping inventory](mapping.md) describes implementation
choices but is not the expectation oracle.

## Reading the evidence

**MATCH** means the selected values agree, including multiplicity. Identifier,
name, address, telecom and report-result repetitions are compared without
requiring their serialization order; given-name parts retain order. Equivalent
timezone spellings compare equal, while the table shows the actual spelling.
**GAP** means a different value or a missing mapped element. A rejected
message has no output Bundle, so all its IG assertions are gaps. **LOCAL**
means the IG supplies no assertion for that behavior; it is not an IG pass.
Arrays show the values selected from the output JSON. `*` visits list members,
numeric indexes start at zero, and `Absent` means the selector found nothing.
Internal references are resolved to aliases such as `Patient[0]` and
`Observation[0]`; raw Bundles retain their original fullUrls.

The profile deliberately uses a local identifier namespace and `OBR-3` as
the accession. For the CX authority comparison, CONF and ALT are assumed
to identify systems, not organizations: the IG allows that interpretation
of CX.4, using HD-to-URI. This is an explicit fixture assumption, not a
claim that every assigning authority must become an identifier system.
Thus PID authority systems and FILL versus ACSN differences
remain visible. `procedure_code_system: LN` still produces hl7bridge's local
procedure-system URI, so agreement of the procedure code alone does not imply
agreement of its system. For the ORM case ORC-2/3 are empty, avoiding the IG's
mutual fallback conditions when both ORC and OBR order numbers are populated.

A04 and A08 use the shared PID/PV1 segment maps by analogy, not a claimed
event-specific IG map. A08 is converted in isolation; this does not test
preservation of existing server fields. A40's inactive prior Patient and
`replaced-by` link are local behavior: the IG's
[MRG map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-mrg-to-account.html)
maps accounts, leaving MRG-1 without a target. The offline A40 Bundle does not
apply a merge to existing server resources; that requires `--post` and is
outside this proof.

The tables cover demographics, visit identifiers/class/time/location,
order code/identifiers/intent/time/requester, and report/result
code/status/value/unit/range/interpretation/time/references. They expose
message-envelope omissions and selected missing data. They do not cover
all segments, data types, terminology combinations, repeat groups, update
sequences or server validation. In particular this is not a FHIR validator
run or evidence of safe use with real patients.

## Rerun

Install the development dependencies from this checkout (Python 3.12+):

```text
python -m pip install -e . pytest ruff mypy types-pyyaml
python tools/check_conformance.py
python tools/check_conformance.py --check
python tools/check_conformance.py --artifacts data/conformance
python -m pytest tests/system/test_conformance.py -q
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

With uv, use `uv sync --locked` and prefix each command with `uv run`.
The script uses the active Python interpreter and this checkout's `src`,
launching `python -m hl7bridge.cli to-fhir` once per bundled sample with the
explicit synthetic profile. This is the same CLI entry point as:

```text
hl7bridge to-fhir tests/fixtures/conformance/adt_a01.hl7 --config tests/fixtures/conformance/profiles
```

There are no downloads during regeneration or tests. `--artifacts` writes
one JSON file per case with the exit code, stderr and unmodified Bundle;
those files are overwritten on a rerun. `--output PATH` writes a report copy.
Successful CLI profile-selection notices are omitted from the tables;
rejection diagnostics use forward slashes for portable paths.
`--check` writes no report and exits 1 on drift. A normal generation exits 0
even with documented gaps; CLI infrastructure errors fail the script.
The test compares freshly generated evidence to this document and checks
successful conversions, references, the local A40 behavior and the SN refusal.
The suite's live FHIR tests skip when no HAPI server is configured.

## Verification recorded on 2026-11-21

Windows, Python 3.12.10: `python -m pytest -q` gave **1232 passed, 48 skipped**.
Linux (Ubuntu on WSL, ext4, Python 3.12): **1237 passed, 43 skipped**; with a
local HAPI server, the whole `tests/system` suite on Linux gave **87 passed**.
Both include all nine conformance tests. Skips cover an unconfigured HAPI server,
optional local reference samples and platform-specific filesystem checks.
`ruff check`, `ruff format --check` and strict `mypy` (69 source files) pass.
After the keep-all-repeats change the PID-3, PID-5, PID-11 and PID-13
repeat assertions match in all six cases; the PID-3.4 system assertion stays a
gap because the MRN system comes from the profile.

<!-- generated conformance evidence -->

## Results

### ADT A01

Synthetic admission; ADT_A01 message and shared segment maps.

Input: [adt_a01.hl7](../tests/fixtures/conformance/adt_a01.hl7) (synthetic).
SHA-256: `8575e23f9d178c04e0c8f07f8184101269ccd4da67ce3477101739658565b486`.

CLI exit: **0**. Resources: 1 Encounter, 1 Patient.

| Input / output selector | IG expectation | hl7bridge produces | Result |
| --- | --- | --- | --- |
| MSH<br>`"Bundle.type"` | Message Bundle envelope ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-msh-to-bundle.html)) `["message"]` | `["transaction"]` | **GAP** |
| MSH<br>`"MessageHeader.*.resourceType"` | Header resource ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-adt-a01-to-bundle.html)) `["MessageHeader"]` | Absent | **GAP** |
| MSH-4<br>`"Provenance.0.resourceType"` | At least source/transformation provenance ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-adt-a01-to-bundle.html)) `["Provenance"]` | Absent | **GAP** |
| PID-3 repetitions<br>`"Patient.0.identifier.*.value"` | Both supplied identifiers ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["MR9001","ALT9001"]` | `["MR9001","ALT9001"]` | **MATCH** |
| PID-3.4 assigning authorities<br>`"Patient.0.identifier.*.system"` | Under the declared system-authority assumption, use ISO OID URIs ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-datatype-cx-to-identifier.html)) `["urn:oid:1.2.3.4","urn:oid:1.2.3.5"]` | `["urn:hl7bridge:conf:mrn","urn:oid:1.2.3.5"]` | **GAP** |
| PID-5 repetitions<br>`"Patient.0.name.*.family"` | Both family names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ZZZTEST","ZZZTESTOLD"]` | `["ZZZTEST","ZZZTESTOLD"]` | **MATCH** |
| PID-5.2/3, first name<br>`"Patient.0.name.0.given.*"` | Given and middle names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ALPHA","B"]` | `["ALPHA","B"]` | **MATCH** |
| PID-7 date<br>`"Patient.0.birthDate"` | Birth date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02"]` | `["1980-01-02"]` | **MATCH** |
| PID-7 time<br>`"Patient.0._birthDate.extension.*.valueDateTime"` | birthTime extension for precision beyond date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02T03:04:05+00:00"]` | Absent | **GAP** |
| PID-8 = F<br>`"Patient.0.gender"` | Administrative gender ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70001-to-administrative-gender.html)) `["female"]` | `["female"]` | **MATCH** |
| PID-11 repetitions<br>`"Patient.0.address.*.city"` | Both addresses (city selected) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["TESTVILLE","OTHERTOWN"]` | `["TESTVILLE","OTHERTOWN"]` | **MATCH** |
| PID-13 repetitions<br>`"Patient.0.telecom.*"` | Email and telephone with system and use (NET is unmapped by the IG, so no use) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | **MATCH** |
| PV1-19<br>`"Encounter.0.identifier.*.value"` | Visit identifier ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["VIS9001"]` | `["VIS9001"]` | **MATCH** |
| PID / PV1 relationship<br>`"Encounter.0.subject.reference"` | Encounter refers to the PID Patient ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-adt-a01-to-bundle.html)) `["Patient[0]"]` | `["Patient[0]"]` | **MATCH** |
| PV1-44<br>`"Encounter.0.period.start"` | Visit start ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["2026-11-21T10:00:00+00:00"]` | `["2026-11-21T10:00:00Z"]` | **MATCH** |
| PV1-3<br>`"Encounter.0.location.*.status"` | Active location for non-planned class; location reference also required ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["active"]` | Absent | **GAP** |
| PV1-2<br>`"Encounter.0.class.code"` | Visit class ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70004-to-v3-actcode.html)) `["IMP"]` | `["IMP"]` | **MATCH** |
| PV1-2 / PV1-45<br>`"Encounter.0.status"` | Visit status ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70004-to-encounter-status.html)) `["in-progress"]` | `["in-progress"]` | **MATCH** |
| OBX-2 NM / OBX-5<br>`"Observation.0.valueQuantity.value"` | Patient observation from the ADT OBX ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-adt-a01-to-bundle.html)) `[190]` | Absent | **GAP** |

### ADT A04

No separate event map in IG 1.0.0; shared PID/PV1 expectations applied by analogy.

Input: [adt_a04.hl7](../tests/fixtures/conformance/adt_a04.hl7) (synthetic).
SHA-256: `0c139ea43553c343e41ec8aabc6d024848e41366d6acf57c44533b36e4b31d8c`.

CLI exit: **0**. Resources: 1 Encounter, 1 Patient.

| Input / output selector | IG expectation | hl7bridge produces | Result |
| --- | --- | --- | --- |
| PID-3 repetitions<br>`"Patient.0.identifier.*.value"` | Both supplied identifiers ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["MR9001","ALT9001"]` | `["MR9001","ALT9001"]` | **MATCH** |
| PID-3.4 assigning authorities<br>`"Patient.0.identifier.*.system"` | Under the declared system-authority assumption, use ISO OID URIs ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-datatype-cx-to-identifier.html)) `["urn:oid:1.2.3.4","urn:oid:1.2.3.5"]` | `["urn:hl7bridge:conf:mrn","urn:oid:1.2.3.5"]` | **GAP** |
| PID-5 repetitions<br>`"Patient.0.name.*.family"` | Both family names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ZZZTEST","ZZZTESTOLD"]` | `["ZZZTEST","ZZZTESTOLD"]` | **MATCH** |
| PID-5.2/3, first name<br>`"Patient.0.name.0.given.*"` | Given and middle names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ALPHA","B"]` | `["ALPHA","B"]` | **MATCH** |
| PID-7 date<br>`"Patient.0.birthDate"` | Birth date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02"]` | `["1980-01-02"]` | **MATCH** |
| PID-7 time<br>`"Patient.0._birthDate.extension.*.valueDateTime"` | birthTime extension for precision beyond date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02T03:04:05+00:00"]` | Absent | **GAP** |
| PID-8 = F<br>`"Patient.0.gender"` | Administrative gender ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70001-to-administrative-gender.html)) `["female"]` | `["female"]` | **MATCH** |
| PID-11 repetitions<br>`"Patient.0.address.*.city"` | Both addresses (city selected) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["TESTVILLE","OTHERTOWN"]` | `["TESTVILLE","OTHERTOWN"]` | **MATCH** |
| PID-13 repetitions<br>`"Patient.0.telecom.*"` | Email and telephone with system and use (NET is unmapped by the IG, so no use) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | **MATCH** |
| PV1-19<br>`"Encounter.0.identifier.*.value"` | Visit identifier ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["VIS9001"]` | `["VIS9001"]` | **MATCH** |
| PID / PV1 relationship<br>`"Encounter.0.subject.reference"` | Encounter refers to the PID Patient ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-adt-a01-to-bundle.html)) `["Patient[0]"]` | `["Patient[0]"]` | **MATCH** |
| PV1-44<br>`"Encounter.0.period.start"` | Visit start ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["2026-11-21T10:00:00+00:00"]` | `["2026-11-21T10:00:00Z"]` | **MATCH** |
| PV1-3<br>`"Encounter.0.location.*.status"` | Active location for non-planned class; location reference also required ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["active"]` | Absent | **GAP** |
| PV1-2<br>`"Encounter.0.class.code"` | Visit class ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70004-to-v3-actcode.html)) `["AMB"]` | `["AMB"]` | **MATCH** |
| PV1-2 / PV1-45<br>`"Encounter.0.status"` | Visit status ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70004-to-encounter-status.html)) `["in-progress"]` | `["in-progress"]` | **MATCH** |

### ADT A08

No separate event map in IG 1.0.0; shared PID/PV1 expectations applied by analogy.

Input: [adt_a08.hl7](../tests/fixtures/conformance/adt_a08.hl7) (synthetic).
SHA-256: `8f7ed0d10c1967d5ea7a62735d41f5ce104ee43e85d7626250c9941cda23e5f5`.

CLI exit: **0**. Resources: 1 Encounter, 1 Patient.

| Input / output selector | IG expectation | hl7bridge produces | Result |
| --- | --- | --- | --- |
| PID-3 repetitions<br>`"Patient.0.identifier.*.value"` | Both supplied identifiers ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["MR9001","ALT9001"]` | `["MR9001","ALT9001"]` | **MATCH** |
| PID-3.4 assigning authorities<br>`"Patient.0.identifier.*.system"` | Under the declared system-authority assumption, use ISO OID URIs ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-datatype-cx-to-identifier.html)) `["urn:oid:1.2.3.4","urn:oid:1.2.3.5"]` | `["urn:hl7bridge:conf:mrn","urn:oid:1.2.3.5"]` | **GAP** |
| PID-5 repetitions<br>`"Patient.0.name.*.family"` | Both family names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ZZZTEST","ZZZTESTOLD"]` | `["ZZZTEST","ZZZTESTOLD"]` | **MATCH** |
| PID-5.2/3, first name<br>`"Patient.0.name.0.given.*"` | Given and middle names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ALPHA","B"]` | `["ALPHA","B"]` | **MATCH** |
| PID-7 date<br>`"Patient.0.birthDate"` | Birth date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02"]` | `["1980-01-02"]` | **MATCH** |
| PID-7 time<br>`"Patient.0._birthDate.extension.*.valueDateTime"` | birthTime extension for precision beyond date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02T03:04:05+00:00"]` | Absent | **GAP** |
| PID-8 = F<br>`"Patient.0.gender"` | Administrative gender ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70001-to-administrative-gender.html)) `["female"]` | `["female"]` | **MATCH** |
| PID-11 repetitions<br>`"Patient.0.address.*.city"` | Both addresses (city selected) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["TESTVILLE","OTHERTOWN"]` | `["TESTVILLE","OTHERTOWN"]` | **MATCH** |
| PID-13 repetitions<br>`"Patient.0.telecom.*"` | Email and telephone with system and use (NET is unmapped by the IG, so no use) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | **MATCH** |
| PV1-19<br>`"Encounter.0.identifier.*.value"` | Visit identifier ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["VIS9001"]` | `["VIS9001"]` | **MATCH** |
| PID / PV1 relationship<br>`"Encounter.0.subject.reference"` | Encounter refers to the PID Patient ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-adt-a01-to-bundle.html)) `["Patient[0]"]` | `["Patient[0]"]` | **MATCH** |
| PV1-44<br>`"Encounter.0.period.start"` | Visit start ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["2026-11-21T10:00:00+00:00"]` | `["2026-11-21T10:00:00Z"]` | **MATCH** |
| PV1-3<br>`"Encounter.0.location.*.status"` | Active location for non-planned class; location reference also required ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["active"]` | Absent | **GAP** |
| PV1-2<br>`"Encounter.0.class.code"` | Visit class ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70004-to-v3-actcode.html)) `["IMP"]` | `["IMP"]` | **MATCH** |
| PV1-2 / PV1-45<br>`"Encounter.0.status"` | Visit status ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["finished"]` | `["finished"]` | **MATCH** |
| PV1-45<br>`"Encounter.0.period.end"` | Updated visit end ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pv1-to-encounter.html)) `["2026-11-21T11:00:00+00:00"]` | `["2026-11-21T11:00:00Z"]` | **MATCH** |

### ADT A40

No IG A40 or MRG-to-Patient map. PID rows apply to the survivor; merge rows are local.

Input: [adt_a40.hl7](../tests/fixtures/conformance/adt_a40.hl7) (synthetic).
SHA-256: `76bf20cc9b1e639f522e9ef76367ef6753dc54bca1897d5c97faa569103321b8`.

CLI exit: **0**. Resources: 2 Patient.

| Input / output selector | IG expectation | hl7bridge produces | Result |
| --- | --- | --- | --- |
| PID-3 repetitions<br>`"Patient.0.identifier.*.value"` | Both supplied identifiers ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["MR9001","ALT9001"]` | `["MR9001","ALT9001"]` | **MATCH** |
| PID-3.4 assigning authorities<br>`"Patient.0.identifier.*.system"` | Under the declared system-authority assumption, use ISO OID URIs ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-datatype-cx-to-identifier.html)) `["urn:oid:1.2.3.4","urn:oid:1.2.3.5"]` | `["urn:hl7bridge:conf:mrn","urn:oid:1.2.3.5"]` | **GAP** |
| PID-5 repetitions<br>`"Patient.0.name.*.family"` | Both family names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ZZZTEST","ZZZTESTOLD"]` | `["ZZZTEST","ZZZTESTOLD"]` | **MATCH** |
| PID-5.2/3, first name<br>`"Patient.0.name.0.given.*"` | Given and middle names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ALPHA","B"]` | `["ALPHA","B"]` | **MATCH** |
| PID-7 date<br>`"Patient.0.birthDate"` | Birth date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02"]` | `["1980-01-02"]` | **MATCH** |
| PID-7 time<br>`"Patient.0._birthDate.extension.*.valueDateTime"` | birthTime extension for precision beyond date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02T03:04:05+00:00"]` | Absent | **GAP** |
| PID-8 = F<br>`"Patient.0.gender"` | Administrative gender ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70001-to-administrative-gender.html)) `["female"]` | `["female"]` | **MATCH** |
| PID-11 repetitions<br>`"Patient.0.address.*.city"` | Both addresses (city selected) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["TESTVILLE","OTHERTOWN"]` | `["TESTVILLE","OTHERTOWN"]` | **MATCH** |
| PID-13 repetitions<br>`"Patient.0.telecom.*"` | Email and telephone with system and use (NET is unmapped by the IG, so no use) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | **MATCH** |
| MRG-1<br>`"Patient.1.identifier.*.value"` | No IG Patient merge target | `["OLD9001"]` | **LOCAL (no IG assertion)** |
| MRG-1<br>`"Patient.1.active"` | No IG active-state assertion | `[false]` | **LOCAL (no IG assertion)** |
| MRG-1<br>`"Patient.1.link.*.type"` | No IG link-type assertion | `["replaced-by"]` | **LOCAL (no IG assertion)** |
| MRG-1 / PID-3<br>`"Patient.1.link.*.other.reference"` | No IG link-target assertion | `["Patient[0]"]` | **LOCAL (no IG assertion)** |
| Offline merge policy<br>`"Bundle.entry.1.request.method"` | Local conditional create; existing prior Patient is not merged by this file | `["POST"]` | **LOCAL (no IG assertion)** |

### ORM O01

Synthetic new order. ORC-2/3 empty; OBR supplies placer and filler identifiers.

Input: [orm_o01.hl7](../tests/fixtures/conformance/orm_o01.hl7) (synthetic).
SHA-256: `dda4e0d9a1410a2dd11729ac58368647e3cc17ec8be207c40044b42997617dee`.

CLI exit: **0**. Resources: 1 Patient, 1 ServiceRequest.

| Input / output selector | IG expectation | hl7bridge produces | Result |
| --- | --- | --- | --- |
| MSH<br>`"Bundle.type"` | Message Bundle envelope ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-msh-to-bundle.html)) `["message"]` | `["transaction"]` | **GAP** |
| MSH<br>`"MessageHeader.*.resourceType"` | Header resource ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-orm-o01-to-bundle.html)) `["MessageHeader"]` | Absent | **GAP** |
| MSH-4<br>`"Provenance.0.resourceType"` | At least source/transformation provenance ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-orm-o01-to-bundle.html)) `["Provenance"]` | Absent | **GAP** |
| PID-3 repetitions<br>`"Patient.0.identifier.*.value"` | Both supplied identifiers ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["MR9001","ALT9001"]` | `["MR9001","ALT9001"]` | **MATCH** |
| PID-3.4 assigning authorities<br>`"Patient.0.identifier.*.system"` | Under the declared system-authority assumption, use ISO OID URIs ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-datatype-cx-to-identifier.html)) `["urn:oid:1.2.3.4","urn:oid:1.2.3.5"]` | `["urn:hl7bridge:conf:mrn","urn:oid:1.2.3.5"]` | **GAP** |
| PID-5 repetitions<br>`"Patient.0.name.*.family"` | Both family names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ZZZTEST","ZZZTESTOLD"]` | `["ZZZTEST","ZZZTESTOLD"]` | **MATCH** |
| PID-5.2/3, first name<br>`"Patient.0.name.0.given.*"` | Given and middle names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ALPHA","B"]` | `["ALPHA","B"]` | **MATCH** |
| PID-7 date<br>`"Patient.0.birthDate"` | Birth date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02"]` | `["1980-01-02"]` | **MATCH** |
| PID-7 time<br>`"Patient.0._birthDate.extension.*.valueDateTime"` | birthTime extension for precision beyond date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02T03:04:05+00:00"]` | Absent | **GAP** |
| PID-8 = F<br>`"Patient.0.gender"` | Administrative gender ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70001-to-administrative-gender.html)) `["female"]` | `["female"]` | **MATCH** |
| PID-11 repetitions<br>`"Patient.0.address.*.city"` | Both addresses (city selected) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["TESTVILLE","OTHERTOWN"]` | `["TESTVILLE","OTHERTOWN"]` | **MATCH** |
| PID-13 repetitions<br>`"Patient.0.telecom.*"` | Email and telephone with system and use (NET is unmapped by the IG, so no use) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | **MATCH** |
| ORC-1 = NW, ORC-5 empty<br>`"ServiceRequest.0.status"` | New order status ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70119-to-request-status.html)) `["active"]` | `["active"]` | **MATCH** |
| ORC-1<br>`"ServiceRequest.0.intent"` | Order intent ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-orc-to-servicerequest.html)) `["order"]` | `["order"]` | **MATCH** |
| OBR-2/3, ORC-2/3 empty<br>`"ServiceRequest.0.identifier.*.type.coding.*.code"` | Filler and placer types (filler first for this comparison) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-servicerequest.html)) `["FILL","PLAC"]` | `["ACSN","PLAC"]` | **GAP** |
| OBR-2/3<br>`"ServiceRequest.0.identifier.*.value"` | Filler and placer values (filler first) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-servicerequest.html)) `["ACC9001","PL9001"]` | `["ACC9001","PL9001"]` | **MATCH** |
| OBR-4.1<br>`"ServiceRequest.0.code.coding.*.code"` | Service code ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-servicerequest.html)) `["51523-9"]` | `["51523-9"]` | **MATCH** |
| OBR-4.3 = LN<br>`"ServiceRequest.0.code.coding.*.system"` | LOINC coding system ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-servicerequest.html)) `["http://loinc.org"]` | `["urn:hl7bridge:conf:procedure-codes:LN"]` | **GAP** |
| ORC-9 with NW<br>`"ServiceRequest.0.authoredOn"` | Order creation time ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-orc-to-servicerequest.html)) `["2026-11-21T10:05:00+00:00"]` | `["2026-11-21T10:05:00Z"]` | **MATCH** |
| OBR-6<br>`"ServiceRequest.0.occurrenceDateTime"` | Requested time ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-servicerequest.html)) `["2026-11-21T10:30:00+00:00"]` | Absent | **GAP** |
| ORC-12<br>`"ServiceRequest.0.requester.reference"` | Requester references the mapped Practitioner ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-orc-to-servicerequest.html)) `["Practitioner[0]"]` | Absent | **GAP** |
| PID / ORC relationship<br>`"ServiceRequest.0.subject.reference"` | Order subject ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-orm-o01-to-bundle.html)) `["Patient[0]"]` | `["Patient[0]"]` | **MATCH** |
| PV1<br>`"Encounter.0.resourceType"` | Visit resource ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-orm-o01-to-bundle.html)) `["Encounter"]` | Absent | **GAP** |

### ORU R01

Synthetic final report with NM, CE and TX results, a result note, and a time interval.

Input: [oru_r01.hl7](../tests/fixtures/conformance/oru_r01.hl7) (synthetic).
SHA-256: `66a7d8837a4d6cf5b9ca2b0cdb21cf71b5c5ba0f3d5733641da79159a822ad06`.

CLI exit: **0**. Resources: 1 DiagnosticReport, 2 Observation, 1 Patient.

| Input / output selector | IG expectation | hl7bridge produces | Result |
| --- | --- | --- | --- |
| MSH<br>`"Bundle.type"` | Message Bundle envelope ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-msh-to-bundle.html)) `["message"]` | `["transaction"]` | **GAP** |
| MSH<br>`"MessageHeader.*.resourceType"` | Header resource ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-oru-r01-to-bundle.html)) `["MessageHeader"]` | Absent | **GAP** |
| MSH-4<br>`"Provenance.0.resourceType"` | At least source/transformation provenance ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-oru-r01-to-bundle.html)) `["Provenance"]` | Absent | **GAP** |
| PID-3 repetitions<br>`"Patient.0.identifier.*.value"` | Both supplied identifiers ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["MR9001","ALT9001"]` | `["MR9001","ALT9001"]` | **MATCH** |
| PID-3.4 assigning authorities<br>`"Patient.0.identifier.*.system"` | Under the declared system-authority assumption, use ISO OID URIs ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-datatype-cx-to-identifier.html)) `["urn:oid:1.2.3.4","urn:oid:1.2.3.5"]` | `["urn:hl7bridge:conf:mrn","urn:oid:1.2.3.5"]` | **GAP** |
| PID-5 repetitions<br>`"Patient.0.name.*.family"` | Both family names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ZZZTEST","ZZZTESTOLD"]` | `["ZZZTEST","ZZZTESTOLD"]` | **MATCH** |
| PID-5.2/3, first name<br>`"Patient.0.name.0.given.*"` | Given and middle names ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["ALPHA","B"]` | `["ALPHA","B"]` | **MATCH** |
| PID-7 date<br>`"Patient.0.birthDate"` | Birth date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02"]` | `["1980-01-02"]` | **MATCH** |
| PID-7 time<br>`"Patient.0._birthDate.extension.*.valueDateTime"` | birthTime extension for precision beyond date ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["1980-01-02T03:04:05+00:00"]` | Absent | **GAP** |
| PID-8 = F<br>`"Patient.0.gender"` | Administrative gender ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70001-to-administrative-gender.html)) `["female"]` | `["female"]` | **MATCH** |
| PID-11 repetitions<br>`"Patient.0.address.*.city"` | Both addresses (city selected) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `["TESTVILLE","OTHERTOWN"]` | `["TESTVILLE","OTHERTOWN"]` | **MATCH** |
| PID-13 repetitions<br>`"Patient.0.telecom.*"` | Email and telephone with system and use (NET is unmapped by the IG, so no use) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-pid-to-patient.html)) `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | `[{"system":"email","value":"zzztest@example.invalid"},{"system":"phone","value":"5550100","use":"home"}]` | **MATCH** |
| OBR-25 = F<br>`"DiagnosticReport.0.status"` | Report status ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70123-queries-to-diagnostic-report-status.html)) `["final"]` | `["final"]` | **MATCH** |
| OBR-4.1<br>`"DiagnosticReport.0.code.coding.*.code"` | Report code ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-diagnosticreport.html)) `["51523-9"]` | `["51523-9"]` | **MATCH** |
| OBR-4.3 = LN<br>`"DiagnosticReport.0.code.coding.*.system"` | LOINC system ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-diagnosticreport.html)) `["http://loinc.org"]` | `["urn:hl7bridge:conf:procedure-codes:LN"]` | **GAP** |
| OBR-3<br>`"DiagnosticReport.0.identifier.*.type.coding.*.code"` | Filler and placer identifier types (OBR-3 and OBR-2; ORC-2/3 empty) ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-diagnosticreport.html)) `["FILL","PLAC"]` | `["ACSN"]` | **GAP** |
| OBR-7 and OBR-8<br>`"DiagnosticReport.0.effectivePeriod.start"` | Interval start when end is supplied ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-diagnosticreport.html)) `["2026-11-21T10:15:00+00:00"]` | Absent | **GAP** |
| OBR-8<br>`"DiagnosticReport.0.effectivePeriod.end"` | Interval end ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-diagnosticreport.html)) `["2026-11-21T10:20:00+00:00"]` | Absent | **GAP** |
| OBR-22<br>`"DiagnosticReport.0.issued"` | Report issue time ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obr-to-diagnosticreport.html)) `["2026-11-21T11:00:00+00:00"]` | `["2026-11-21T11:00:00Z"]` | **MATCH** |
| PID / OBR relationship<br>`"DiagnosticReport.0.subject.reference"` | Report subject ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-oru-r01-to-bundle.html)) `["Patient[0]"]` | `["Patient[0]"]` | **MATCH** |
| Three OBX segments<br>`"DiagnosticReport.0.result.*.reference"` | Three linked results ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-oru-r01-to-bundle.html)) `["Observation[0]","Observation[1]","Observation[2]"]` | `["Observation[0]","Observation[1]"]` | **GAP** |
| OBX[1]-3<br>`"Observation.0.code.coding.*.code"` | Result code ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["6153-1"]` | `["6153-1"]` | **MATCH** |
| OBX[1]-3.3 = LN<br>`"Observation.0.code.coding.*.system"` | Result coding system ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["http://loinc.org"]` | `["http://loinc.org"]` | **MATCH** |
| OBX[1]-2 = NM, OBX[1]-5<br>`"Observation.0.valueQuantity.value"` | Numeric value ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `[3.9]` | `[3.9]` | **MATCH** |
| OBX[1]-6<br>`"Observation.0.valueQuantity.code"` | UCUM unit code ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["k[IU]/L"]` | `["k[IU]/L"]` | **MATCH** |
| OBX[1]-6.3 = UCUM<br>`"Observation.0.valueQuantity.system"` | UCUM system ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["http://unitsofmeasure.org"]` | `["http://unitsofmeasure.org"]` | **MATCH** |
| OBX[1]-7<br>`"Observation.0.referenceRange.*.text"` | Reference range ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["<0.10"]` | `["<0.10"]` | **MATCH** |
| OBX[1]-8 = A<br>`"Observation.0.interpretation.*.coding.*.code"` | Abnormal interpretation ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70078-to-v3-observationinterpretation.html)) `["A"]` | `["A"]` | **MATCH** |
| OBX[1]-11 = F<br>`"Observation.0.status"` | Result status ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70085-to-observation-status.html)) `["final"]` | `["final"]` | **MATCH** |
| OBX[1]-14<br>`"Observation.0.effectiveDateTime"` | Result time ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["2026-11-21T10:15:00+00:00"]` | `["2026-11-21T10:15:00Z"]` | **MATCH** |
| OBX[1] / PID relationship<br>`"Observation.0.subject.reference"` | Result subject ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-oru-r01-to-bundle.html)) `["Patient[0]"]` | `["Patient[0]"]` | **MATCH** |
| NTE after OBX[1]<br>`"Observation.0.note.*.text"` | Result annotation ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-nte-to-observation.html)) `["Synthetic specimen note"]` | `["Synthetic specimen note"]` | **MATCH** |
| OBX[2]-2 = CE, OBX[2]-5<br>`"Observation.1.valueCodeableConcept.coding.*.code"` | Coded result ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["260385009"]` | `["260385009"]` | **MATCH** |
| OBX[2]-5.3 = SCT<br>`"Observation.1.valueCodeableConcept.coding.*.system"` | Coded result system ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["http://snomed.info/sct"]` | `["http://snomed.info/sct"]` | **MATCH** |
| OBX[3]-2 = TX, OBX[3]-5<br>`"Observation.2.valueString"` | Text result as Observation.valueString; bridge instead uses report conclusion ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `["SYNTHETIC REPORT ONLY"]` | Absent | **GAP** |
| PV1<br>`"Encounter.0.resourceType"` | Visit resource ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-message-oru-r01-to-bundle.html)) `["Encounter"]` | Absent | **GAP** |

### ORU R01, SN rejection probe

Additional synthetic ORU with a valid comparator and number in separate SN components.

Input: [oru_r01_sn.hl7](../tests/fixtures/conformance/oru_r01_sn.hl7) (synthetic).
SHA-256: `43c9b5a135fc8c90d35bb78334e2526eeadb5d32a6cf04c0240e2c28252a6cac`.

CLI exit: **1**. Resources: none.

```text
using profiles: tests/fixtures/conformance/profiles (--config)
error: oru_r01_sn.hl7: OBX-2: code not in table 0125 (value type); refusing to guess
1 of 1 message(s) could not be mapped
```

| Input / output selector | IG expectation | hl7bridge produces | Result |
| --- | --- | --- | --- |
| OBX-2 = SN, OBX-5 = <^0.10<br>`"Observation.0.valueQuantity.value"` | Structured numeric value ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-segment-obx-to-observation.html)) `[0.1]` | No Bundle: CLI rejected the message | **GAP (rejected)** |
| OBX-5.1 = <<br>`"Observation.0.valueQuantity.comparator"` | Structured numeric comparator via SN-to-Quantity ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-datatype-sn-to-quantity.html)) `["<"]` | No Bundle: CLI rejected the message | **GAP (rejected)** |
| OBR-25 = F<br>`"DiagnosticReport.0.status"` | Report status ([map](https://hl7.org/fhir/uv/v2mappings/STU1/ConceptMap-table-hl70123-queries-to-diagnostic-report-status.html)) `["final"]` | No Bundle: CLI rejected the message | **GAP (rejected)** |

## Counts

GAP: 37; GAP (rejected): 3; LOCAL (no IG assertion): 5; MATCH: 81.

Counts describe only the selected assertions, including repeated checks across samples.
They are not a conformance percentage or a certification result.
