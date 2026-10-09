"""hl7time: aware-only parsing, explicit offsets win, DST is deterministic."""

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from hl7bridge.errors import TimestampError
from hl7bridge.hl7time import format_hl7_ts, parse_hl7_ts

CHICAGO = ZoneInfo("America/Chicago")


def test_full_precision_no_offset_uses_hospital_tz():
    dt = parse_hl7_ts("20261014093000", default_tz=CHICAGO)
    assert dt == datetime(2026, 10, 14, 9, 30, 0, tzinfo=CHICAGO)
    assert dt.utcoffset() == timedelta(hours=-5)  # CDT


def test_explicit_offset_wins_over_default_tz():
    dt = parse_hl7_ts("20261014093000-0700", default_tz=CHICAGO)
    assert dt.utcoffset() == timedelta(hours=-7)
    assert dt.astimezone(UTC) == datetime(2026, 10, 14, 16, 30, tzinfo=UTC)


def test_date_only_is_midnight_local():
    dt = parse_hl7_ts("20261014", default_tz=CHICAGO)
    assert dt == datetime(2026, 10, 14, 0, 0, tzinfo=CHICAGO)


def test_fractional_seconds():
    dt = parse_hl7_ts("20261014093000.1234", default_tz=UTC)
    assert dt.microsecond == 123400


def test_result_is_always_aware():
    for value in ("20261014", "202610140930", "20261014093000.5+0000"):
        dt = parse_hl7_ts(value, default_tz=CHICAGO)
        assert dt.tzinfo is not None and dt.utcoffset() is not None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "2026",  # year only: below minimum precision
        "202610",  # year-month only
        "2026101",  # odd length
        "20261301000000",  # month 13
        "20261014250000",  # hour 25
        "2026101409300",  # 13 digits
        "20261014093000+2500",  # offset hours out of range
        "20261014093000+07",  # short offset
        "14-10-2026",
        "20261014T093000",
        "2026131599",  # seeded malformed-timestamp defect shape
    ],
)
def test_malformed_raises(bad):
    with pytest.raises(TimestampError):
        parse_hl7_ts(bad, default_tz=UTC)


def test_dst_spring_forward_nonexistent_time_is_deterministic():
    # 2026-03-08 02:30 America/Chicago does not exist (spring forward).
    # PEP 495 fold=0 maps it onto a stable UTC instant; what matters here is
    # determinism and awareness, and that it sits between the surrounding hours.
    dt = parse_hl7_ts("20260308023000", default_tz=CHICAGO)
    before = parse_hl7_ts("20260308015959", default_tz=CHICAGO)
    after = parse_hl7_ts("20260308030000", default_tz=CHICAGO)
    assert before.astimezone(UTC) < dt.astimezone(UTC)
    assert dt.astimezone(UTC) <= after.astimezone(UTC) + timedelta(hours=1)


def test_dst_fall_back_ambiguous_time_takes_first_occurrence():
    # 2026-11-01 01:30 America/Chicago happens twice; fold=0 is the first (CDT).
    dt = parse_hl7_ts("20261101013000", default_tz=CHICAGO)
    assert dt.utcoffset() == timedelta(hours=-5)


def test_format_round_trip_with_offset():
    dt = datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert format_hl7_ts(dt, include_offset=True) == "20261014093000-0700"
    assert format_hl7_ts(dt, include_offset=False) == "20261014093000"
    assert parse_hl7_ts(format_hl7_ts(dt, include_offset=True), default_tz=UTC) == dt


def test_format_rejects_naive_datetime():
    with pytest.raises(TimestampError):
        format_hl7_ts(datetime(2026, 10, 14, 9, 30), include_offset=True)
