"""Lint rules: every rule has a fires case and a does-not-fire case.

The exact-set precision test over the full generated corpus lives in
tests/system/test_lint_corpus.py; these are the per-rule contracts.
"""

from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.errors import ConfigError
from hl7bridge.lint import lint_text
from hl7bridge.lint.runner import lint_directory

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "hospitals"


@pytest.fixture(scope="module")
def registry():
    return HospitalRegistry.load_dir(CONFIG_DIR)


def msg(
    *segments: str,
    msg_type: str = "ORM^O01",
    control_id: str = "C1",
    facility: str = "HOSPA",
    ts: str = "20261007100000",
) -> str:
    msh = f"MSH|^~\\&|RIS|{facility}|BRIDGE|HUB|{ts}||{msg_type}|{control_id}|P|2.3"
    return "\r".join([msh, *segments]) + "\r"


PID = "PID|1||M1001^^^HOSPA^MR||ZZZTEST^ALPHA||19700101|F"
ORC = "ORC|NW|PL1|FIL1||||||20261007093000"
OBR_ORM = "OBR|1|PL1|ACC123|71250^CT CHEST^local|||20261007100000|||||||||||||||||CT"
OBR_ORU = (
    "OBR|1|PL1|ACC123|71250^CT CHEST^local|||20261007100000|||||||||||||||20261007103000||CT|F"
)


def rules_of(findings):
    return sorted(f.rule_id for f in findings)


GOOD_ORM = msg(PID, ORC, OBR_ORM)
GOOD_ORU = msg(PID, "ORC|RE|PL1|FIL1", OBR_ORU, msg_type="ORU^R01")
GOOD_ADT = msg(PID, msg_type="ADT^A04")
GOOD_ACK = msg("MSA|AA|RPT1", msg_type="ACK^R01")


@pytest.mark.parametrize("good", [GOOD_ORM, GOOD_ORU, GOOD_ADT, GOOD_ACK])
def test_clean_messages_produce_zero_findings(registry, good):
    assert lint_text(good, registry) == []


def test_hl7_001_unparseable(registry):
    assert rules_of(lint_text("not hl7 at all", registry)) == ["HL7-001"]


def test_hl7_002_missing_control_id(registry):
    bad = GOOD_ORM.replace("|C1|", "||")
    assert "HL7-002" in rules_of(lint_text(bad, registry))


def test_hl7_003_unknown_facility_stops_hospital_rules(registry):
    bad = msg(PID, ORC, OBR_ORM, facility="NOWHERE")
    assert rules_of(lint_text(bad, registry)) == ["HL7-003"]


def test_hl7_004_unexpected_type(registry):
    bad = msg(PID, msg_type="SIU^S12")
    assert rules_of(lint_text(bad, registry)) == ["HL7-004"]


def test_hl7_005_bad_msh7_and_006_missing_msh7(registry):
    assert "HL7-005" in rules_of(lint_text(msg(PID, ORC, OBR_ORM, ts="2026131599"), registry))
    assert "HL7-006" in rules_of(lint_text(msg(PID, ORC, OBR_ORM, ts=""), registry))


def test_hl7_010_missing_accession_respects_dialect(registry):
    # HOSPA reads OBR-3; blanking it fires even though ORC-3 is filled.
    obr_no_acc = OBR_ORM.replace("|ACC123|", "||")
    assert "HL7-010" in rules_of(lint_text(msg(PID, ORC, obr_no_acc), registry))
    # HOSPB reads ORC-3; the same OBR-3 blank message with ORC-3 filled is clean there.
    assert "HL7-010" not in rules_of(
        lint_text(msg(PID, ORC, obr_no_acc, facility="HOSPB"), registry)
    )


def test_hl7_011_missing_procedure_code(registry):
    obr_no_code = OBR_ORM.replace("|71250^CT CHEST^local|", "||")
    findings = lint_text(msg(PID, ORC, obr_no_code), registry)
    assert "HL7-011" in rules_of(findings)
    assert next(f for f in findings if f.rule_id == "HL7-011").severity == "warning"


def test_hl7_012_missing_pid3(registry):
    pid_no_mrn = "PID|1||||ZZZTEST^ALPHA||19700101|F"
    assert "HL7-012" in rules_of(lint_text(msg(pid_no_mrn, ORC, OBR_ORM), registry))
    assert "HL7-012" in rules_of(lint_text(msg(pid_no_mrn, msg_type="ADT^A04"), registry))


def test_hl7_013_bad_report_status(registry):
    bad = GOOD_ORU.replace("|CT|F", "|CT|X")
    assert "HL7-013" in rules_of(lint_text(bad, registry))


def test_hl7_014_bad_obr22(registry):
    bad = GOOD_ORU.replace("20261007103000", "2026131599")
    assert "HL7-014" in rules_of(lint_text(bad, registry))
    # Absent OBR-22 is legal, not a finding.
    absent = GOOD_ORU.replace("20261007103000", "")
    assert "HL7-014" not in rules_of(lint_text(absent, registry))


def test_hl7_015_bad_obr7(registry):
    bad = GOOD_ORU.replace("|||20261007100000", "|||2026139999")
    assert "HL7-015" in rules_of(lint_text(bad, registry))


def test_hl7_016_unsupported_order_control(registry):
    bad = msg(PID, ORC.replace("ORC|NW", "ORC|XX"), OBR_ORM)
    assert "HL7-016" in rules_of(lint_text(bad, registry))


def test_hl7_020_ack_without_msa(registry):
    assert "HL7-020" in rules_of(lint_text(msg(msg_type="ACK^R01"), registry))
    assert "HL7-020" in rules_of(lint_text(msg("MSA|XX|RPT1", msg_type="ACK^R01"), registry))


def test_disabled_rules_filter_findings(tmp_path, registry):
    cfg_text = (CONFIG_DIR / "hospa.yaml").read_text(encoding="utf-8")
    needle = "lint:\n  disabled_rules: []"
    assert needle in cfg_text
    (tmp_path / "h.yaml").write_text(
        cfg_text.replace(needle, "lint:\n  disabled_rules: [HL7-011]"), encoding="utf-8"
    )
    custom = HospitalRegistry.load_dir(tmp_path)
    obr_no_code = OBR_ORM.replace("|71250^CT CHEST^local|", "||")
    assert rules_of(lint_text(msg(PID, ORC, obr_no_code), custom)) == []


def test_unknown_disabled_rule_rejected_at_lint_time(tmp_path):
    cfg_text = (CONFIG_DIR / "hospa.yaml").read_text(encoding="utf-8")
    needle = "lint:\n  disabled_rules: []"
    (tmp_path / "h.yaml").write_text(
        cfg_text.replace(needle, "lint:\n  disabled_rules: [HL7-999]"), encoding="utf-8"
    )
    custom = HospitalRegistry.load_dir(tmp_path)  # shape passes config load
    with pytest.raises(ConfigError, match="HL7-999"):
        lint_text(GOOD_ORM, custom)


def test_lint_directory_empty_rejected(tmp_path, registry):
    with pytest.raises(ConfigError, match=r"no .*hl7 files"):
        lint_directory(tmp_path, registry)
