# ADR-0001: HL7 v2 parsing with python-hl7

Date: 2026-10-09
Status: accepted

## Context

Two libraries were candidates: `hl7apy` and `python-hl7`. We need: tolerant parsing of
per-hospital dialects (accession in OBR-3 vs ORC-3, Z-segments), positional
field addressing driven by config strings like `"OBR-3"`, correct escape
handling, and loud failures on garbage. We run our own asyncio MLLP server
either way, because ACK decisions and quarantine are the product here.

## Evidence (spike run 2026-10-09, Python 3.12.3, hl7apy 1.3.5, python-hl7 0.4.5)

Both parsed a v2.3 ORU with a Z-segment and repetitions, and both raised on
garbage input. Differences that decided it:

- `python-hl7` addresses MSH by spec position (`msh[9] == 'ORU^R01'`) and
  `extract_field('OBR', 1, 3, 1, 1)` maps 1:1 onto our config-driven
  `"SEG-N"` references. `extract_field` also unescapes exactly once
  (`\T\` -> `&`), verified by test (double-unescape is a silent-corruption
  class we test against).
- `hl7apy.to_er7()` returns still-escaped text (`Tom \T\ Jerry`), so every
  read site would need its own unescape pass.
- `hl7apy` carries full per-version message definitions and validation
  machinery. Our dialect reality is that hospitals deviate from those
  definitions; we would be configuring exceptions to a model we do not use.
  `python-hl7` is a small positional parser, which is the actual job.

## Decision

`python-hl7` (`hl7` on PyPI), wrapped in `hl7bridge.ingest.parser` so the
rest of the code never touches library API directly. MLLP framing is our own
(`hl7bridge.ingest.mllp`): ~60 lines, fully unit-tested, and it reports junk
bytes instead of dropping them, which neither library's transport helper
guarantees.

## Consequences

- No message-definition validation; required-field checks live in the
  pipeline per message type, where the per-hospital rules are anyway.
- A library swap is contained to `ingest/parser.py` (one test file proves
  the addressing contract).
