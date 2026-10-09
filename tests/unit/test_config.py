"""Config loading is strict: unknown keys, bad values, and collisions fail loudly."""

from pathlib import Path

import pytest

from hl7bridge.config import HospitalRegistry, load_hospital_config
from hl7bridge.errors import ConfigError

GOOD = """\
hospital_id: HOSPA
display_name: Hospital A (synthetic)
facility_codes: [HOSPA, HOSPA_RIS]
accession_field: OBR-3
timezone: America/Los_Angeles
expected_message_types: [ADT^A04, ORM^O01, ORU^R01]
procedure_code_system: local
reconcile:
  order_without_image_hours: 4
  order_without_image_hours_by_modality:
    US: 2
  order_lag_minutes: 45
ack_timeout_minutes: 20
"""


def write(tmp_path: Path, text: str, name: str = "h.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_good_config_loads(tmp_path):
    cfg = load_hospital_config(write(tmp_path, GOOD))
    assert cfg.hospital_id == "HOSPA"
    assert cfg.facility_codes == ("HOSPA", "HOSPA_RIS")
    assert cfg.accession_field == "OBR-3"
    assert cfg.tzinfo.key == "America/Los_Angeles"
    assert cfg.reconcile.threshold_hours("US") == 2.0
    assert cfg.reconcile.threshold_hours("CT") == 4.0
    assert cfg.reconcile.order_lag_minutes == 45.0
    assert cfg.ack_timeout_minutes == 20.0
    assert cfg.timestamps_include_offset is False


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"unknown keys.*accesion_field"):
        load_hospital_config(write(tmp_path, GOOD + "accesion_field: OBR-3\n"))


def test_unknown_reconcile_key_rejected(tmp_path):
    bad = GOOD.replace("order_lag_minutes: 45", "order_lag_mins: 45")
    with pytest.raises(ConfigError, match="unknown reconcile keys"):
        load_hospital_config(write(tmp_path, bad))


def test_bad_timezone_rejected(tmp_path):
    bad = GOOD.replace("America/Los_Angeles", "America/Nowhere")
    with pytest.raises(ConfigError, match="unknown IANA timezone"):
        load_hospital_config(write(tmp_path, bad))


def test_bad_accession_field_rejected(tmp_path):
    bad = GOOD.replace("accession_field: OBR-3", "accession_field: OBR-18")
    with pytest.raises(ConfigError, match="accession_field"):
        load_hospital_config(write(tmp_path, bad))


def test_empty_facility_codes_rejected(tmp_path):
    bad = GOOD.replace("facility_codes: [HOSPA, HOSPA_RIS]", "facility_codes: []")
    with pytest.raises(ConfigError, match="facility_codes"):
        load_hospital_config(write(tmp_path, bad))


def test_registry_duplicate_facility_code_rejected(tmp_path):
    write(tmp_path, GOOD, "a.yaml")
    other = GOOD.replace("hospital_id: HOSPA", "hospital_id: HOSPB")
    write(tmp_path, other, "b.yaml")  # same facility codes as HOSPA
    with pytest.raises(ConfigError, match="facility code"):
        HospitalRegistry.load_dir(tmp_path)


def test_registry_empty_dir_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"no .*hospital configs"):
        HospitalRegistry.load_dir(tmp_path)


def test_registry_facility_matching(tmp_path):
    write(tmp_path, GOOD, "a.yaml")
    reg = HospitalRegistry.load_dir(tmp_path)
    assert reg.match_facility("HOSPA_RIS").hospital_id == "HOSPA"
    assert reg.match_facility("NOWHERE", "HOSPA").hospital_id == "HOSPA"
    assert reg.match_facility("NOWHERE") is None


def test_repo_hospital_configs_load():
    """The shipped config/hospitals directory must always be loadable."""
    root = Path(__file__).resolve().parents[2]
    reg = HospitalRegistry.load_dir(root / "config" / "hospitals")
    assert set(reg.by_id) == {"HOSPA", "HOSPB", "HOSPC"}
    # The dialect spread the demo depends on: differing accession fields.
    fields = {h.accession_field for h in reg.by_id.values()}
    assert len(fields) >= 2
