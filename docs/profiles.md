# Hospital profiles

A profile is one YAML file that describes one hospital's HL7 dialect: where
it puts the accession number, which time zone its timestamps use, which
message types it sends, and the thresholds the reports use. The code never
hard-codes a field position; everything that differs between sites lives
here.

## Where hl7bridge looks

1. `--config DIR`
2. the `HL7BRIDGE_CONFIG` environment variable
3. `./hl7bridge/profiles` (what `hl7bridge init` writes)
4. the profiles bundled inside the package

Every command prints the folder it used on stderr (`using profiles: ...`).
`-q` turns that line off.

## Bundled profiles

| File | Dialect it imitates |
| --- | --- |
| `hospa.yaml` | v2.3, accession in OBR-3, local time with no UTC offset |
| `hospb.yaml` | v2.4, accession in ORC-3, a Z-segment on orders, local time |
| `hospc.yaml` | v2.5, accession in OBR-3 (ORC-3 filled too), explicit UTC offsets, ACKs for outbound reports, inpatient visits (ADT^A01) |

All three are synthetic. `hl7bridge init` copies them, with fifteen sample
messages, into a folder you can edit.

## Commands

- `hl7bridge profile check [DIR]` validates every file on its own, then the
  set together (duplicate hospital ids and facility codes), and prints one
  line per file. It exits 1 if any file fails.
- `hl7bridge profile show [HOSPITAL ...]` prints the effective values with
  every default filled in. `--json` prints JSON. The YAML it prints loads
  back to the same profile.

## Keys

Validation is strict: an unknown key fails the load, and a near miss gets a
suggestion (`unknown key 'acession_field'. Did you mean 'accession_field'?`).

| Key | Type | Default | Why a hospital needs it |
| --- | --- | --- | --- |
| `hospital_id` | string | required | The id every report, quarantine row and FHIR identifier system uses for this site |
| `display_name` | string | required | A readable name for report headings |
| `facility_codes` | list of strings | required | The MSH-4 or MSH-6 values this site sends; a message is matched to the first profile that claims its code |
| `accession_field` | one of `OBR-2`, `OBR-3`, `ORC-2`, `ORC-3` | required | Sites disagree on where the accession lives; reconciliation and the ACK decision read it from here |
| `timezone` | IANA zone name | required | Timestamps with no UTC offset are read in this zone; month boundaries in the TAT report use it too |
| `expected_message_types` | list of `TYPE^TRIGGER` | required | Anything else gets AR `unexpected_message_type`, so a feed that starts sending new events is noticed |
| `hl7_version` | string | `"2.3"` | Echoed in ACKs and used by the generator; tells a reader which version the site claims |
| `timestamps_include_offset` | boolean | `false` | Whether the site sends `-0400` style offsets; the generator and the linter follow it |
| `procedure_code_system` | string | `local` | The code system of OBR-4 procedure codes, carried into FHIR |
| `z_segment` | string | none | A site-specific Z-segment appended to orders, so the parser is tested against one |
| `ack_timeout_minutes` | positive number | `30` | How long an outbound report may wait for its ACK before it counts as NO_ACK |
| `reconcile.order_without_image_hours` | number | `4` | How old an order may get with no study before it is flagged |
| `reconcile.order_without_image_hours_by_modality` | map of modality to hours | `{}` | Per-modality overrides, since ultrasound is done sooner than MR |
| `reconcile.order_lag_minutes` | number | `60` | Grace period before a study with no order is flagged, for orders that arrive late |
| `tat.exam_to_final_target_hours` | number | `2` | The turnaround target; a report strictly over it is a breach |
| `tat.exam_to_final_target_hours_by_priority` | map of priority to hours | `{}` | Per-priority targets, such as a tighter one for stat (`S`) |
| `lint.disabled_rules` | list of rule ids | `[]` | Rules this site's feed is known to break, so lint output stays useful |
| `fhir.identifier_systems.mrn` | `urn:` or `http(s)` URI | `urn:hl7bridge:<hospital_id lower case>:mrn` | The FHIR identifier system for PID-3 MRNs, so Patients match the ones the server already holds |
| `fhir.identifier_systems.accession` | `urn:` or `http(s)` URI | `urn:hl7bridge:<hospital_id lower case>:accession` | The system for the accession identifier (type ACSN) on ServiceRequest, DiagnosticReport and ImagingStudy |
| `fhir.identifier_systems.placer` | `urn:` or `http(s)` URI | `urn:hl7bridge:<hospital_id lower case>:placer` | The system for the placer order number (ORC-2, type PLAC) |
| `fhir.identifier_systems.visit` | `urn:` or `http(s)` URI | `urn:hl7bridge:<hospital_id lower case>:visit` | The system for the visit number (PV1-19, type VN) on Encounter |
| `fhir.identifier_systems.observation` | `urn:` or `http(s)` URI | `urn:hl7bridge:<hospital_id lower case>:observation` | The system for the result key `<accession>/<OBX-3.1>/sub/<OBX-4>` (or `.../set/<OBX-1>` when OBX-4 is empty) on Observation |

| `fhir.identifier_systems.mrn_by_authority` | map of PID-3.4 authority to URI | `{}` | Direct mode picks the MRN system by the assigning authority, so two authorities in one feed match the right Patients. The key is the namespace (PID-3.4 subcomponent 1), decoded and trimmed: a separator the sender escaped in it is part of the key (`ST&MARY`) |
| `fhir.patient_policy` | `create` or `match_only` | `create` | `listen --target fhir` takes `match_only` only: it finds Patients by MRN and never writes one |
| `fhir.tag` | `{system: URI, code: token}` | `{system: urn:hl7bridge:tag, code: hl7v2-lab}` in direct mode | The lab tag on every direct write; the run tag's system is this system plus `/lab-run` |
| `fhir.allowed_processing_ids` | list of MSH-11 codes | `[T, D]` | Direct mode refuses any other MSH-11 with AR, so production traffic never reaches a lab server |

Numbers may be written as integers or decimals; `profile show` prints them
as decimals.

`resend_ignores` is not a key: a resend that differs in any field, MSH-7
included, is a conflict, never a duplicate. The example profile for a
teaching lab is `src/hl7bridge/profiles/examples/wintehr.yaml`; it is not
among the bundled profiles that load by default.

An identifier system may not hold `|`, `,`, `$`, a backslash or a space:
FHIR token searches treat them as separators, and the conditional update
URL `identifier=<system>|<value>` would break. Every key under `fhir` is
optional; a profile with no `fhir` block writes the default systems, as
0.3.0 did. `docs/mapping.md` lists what each system is written on.
