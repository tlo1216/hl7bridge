"""Parser wrapper: spec-positional addressing, single unescape, loud failures."""

import pytest

from hl7bridge.errors import HL7ParseError
from hl7bridge.ingest.parser import (
    collect_obx_text,
    get_control_id,
    get_facilities,
    get_field,
    get_message_type,
    get_version,
    parse_hl7,
    split_field_ref,
)

ORU = (
    "MSH|^~\\&|RIS|HOSPA|EHR|BAYIMG|20261014093000||ORU^R01|MSG00001|P|2.3\r"
    "PID|1||123456^^^HOSPA^MR~999888^^^STATE^SS||ZZZTEST^ALPHA||19700101|F\r"
    "ORC|RE|PLACER1|FILLER1\r"
    "OBR|1|ORD123|ACC987654|71250^CT CHEST WO CONTRAST^C4|||20261014080000"
    "|||||||||1234^READER^SYNTH||||||20261014092500||CT|F\r"
    "OBX|1|TX|IMPRESSION||Tom \\T\\ Jerry: no acute findings.||||||F\r"
    "OBX|2|TX|IMPRESSION||Second line.||||||F\r"
)


def test_msh_fields_use_spec_positions():
    m = parse_hl7(ORU)
    assert get_message_type(m) == "ORU^R01"
    assert get_control_id(m) == "MSG00001"
    assert get_facilities(m) == ("HOSPA", "BAYIMG")
    assert get_version(m) == "2.3"


def test_field_addressing_and_components():
    m = parse_hl7(ORU)
    assert get_field(m, "OBR-3") == "ACC987654"
    assert get_field(m, "ORC-3") == "FILLER1"
    assert get_field(m, "OBR-4", component=2) == "CT CHEST WO CONTRAST"
    assert get_field(m, "PID-3", repetition=2, component=1) == "999888"
    assert get_field(m, "OBR-25") == "F"


def test_absent_fields_return_empty_string():
    m = parse_hl7(ORU)
    assert get_field(m, "OBR-18") == ""
    assert get_field(m, "ZZZ-1") == ""  # absent segment
    assert get_field(m, "PID-3", repetition=5) == ""


def test_unescape_happens_exactly_once():
    m = parse_hl7(ORU)
    # \T\ must decode to '&' and never be double-processed.
    assert get_field(m, "OBX-5") == "Tom & Jerry: no acute findings."


def test_collect_obx_text_joins_lines():
    m = parse_hl7(ORU)
    assert collect_obx_text(m) == "Tom & Jerry: no acute findings.\nSecond line."


def test_newline_variants_normalized():
    m = parse_hl7(ORU.replace("\r", "\n"))
    assert get_control_id(m) == "MSG00001"
    m = parse_hl7(ORU.replace("\r", "\r\n"))
    assert get_control_id(m) == "MSG00001"


@pytest.mark.parametrize("bad", ["", "   ", "garbage with no segments", "XYZ|1|2\r"])
def test_unparseable_raises(bad):
    with pytest.raises(HL7ParseError):
        parse_hl7(bad)


def test_split_field_ref():
    assert split_field_ref("OBR-3") == ("OBR", 3)
    assert split_field_ref("MSH-10") == ("MSH", 10)
    with pytest.raises(ValueError, match="bad HL7 field reference"):
        split_field_ref("OBR3")
