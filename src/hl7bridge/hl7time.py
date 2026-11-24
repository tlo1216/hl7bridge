"""HL7 DTM timestamp parsing and formatting.

Policy (UTC inside, aware timestamps only):
- Every parsed timestamp is timezone-aware. If the value carries an explicit
  ``+HHMM`` / ``-HHMM`` offset, that offset wins. Otherwise the caller's
  default zone is attached: the MSH-7 offset of the message when it has one
  (``message_tz``; HL7 v2 makes it the message default), else the
  hospital's configured timezone (per-hospital config, never a guess).
- Minimum accepted precision is a full date (``YYYYMMDD``). Anything shorter,
  longer, or non-numeric raises :class:`TimestampError`. Nothing is coerced.
- A date of birth (PID-7, ``parse_birth``) may also be a year or a year and
  month, kept at that precision, as a FHIR date allows.
- Local times that fall inside a DST transition resolve per PEP 495 with
  ``fold=0`` (the first occurrence of an ambiguous wall time, and the standard
  mapping for nonexistent spring-forward times). Deterministic and documented,
  not silent: callers that care can compare offsets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from .errors import TimestampError

_TS_RE = re.compile(
    r"^(?P<body>\d{8}|\d{10}|\d{12}|\d{14})"
    r"(?:\.(?P<frac>\d{1,4}))?"
    r"(?P<off>[+-]\d{4})?$"
)


def parse_hl7_ts(value: str, *, default_tz: tzinfo) -> datetime:
    """Parse an HL7 DTM value into an aware datetime.

    Raises TimestampError on empty, malformed, or sub-date-precision values.
    """
    v = value.strip()
    if not v:
        raise TimestampError("empty timestamp")
    m = _TS_RE.match(v)
    if m is None:
        raise TimestampError(f"malformed HL7 timestamp: {v!r}")
    body = m.group("body")
    padded = body.ljust(14, "0")
    try:
        dt = datetime(  # noqa: DTZ001 - tzinfo attached below; naive never escapes
            year=int(padded[0:4]),
            month=int(padded[4:6]),
            day=int(padded[6:8]),
            hour=int(padded[8:10]),
            minute=int(padded[10:12]),
            second=int(padded[12:14]),
        )
    except ValueError as exc:
        raise TimestampError(f"out-of-range HL7 timestamp: {v!r} ({exc})") from exc

    frac = m.group("frac")
    if frac is not None:
        # HL7 allows up to 4 fractional-second digits; pad to microseconds.
        dt = dt.replace(microsecond=int(frac.ljust(6, "0")))

    off = m.group("off")
    if off is not None:
        sign = 1 if off[0] == "+" else -1
        hours, minutes = int(off[1:3]), int(off[3:5])
        if hours > 23 or minutes > 59:
            raise TimestampError(f"invalid UTC offset in HL7 timestamp: {v!r}")
        return dt.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    return dt.replace(tzinfo=default_tz)


# ASCII digits only: the year, month and fraction are copied as sent.
_BIRTH_RE = re.compile(
    r"^(?P<body>\d{4}|\d{6}|\d{8}|\d{10}|\d{12}|\d{14})"
    r"(?:\.(?P<frac>\d{1,4}))?"
    r"(?P<off>[+-]\d{4})?$",
    re.ASCII,
)
# FHIR's dateTime offsets: -14:00 to +14:00 (R4 datatypes regex).
_MAX_FHIR_OFFSET = timedelta(hours=14)


@dataclass(frozen=True, slots=True)
class BirthValue:
    """PID-7 read for FHIR: ``date`` is a FHIR date at the precision sent
    (``1980``, ``1980-01`` or ``1980-01-02``); ``time`` is a FHIR dateTime
    for the patient-birthTime extension, or None when PID-7 has no minutes."""

    date: str
    time: str | None


def parse_birth(value: str, *, default_tz: tzinfo) -> BirthValue:
    """Read an HL7 date/time of birth (PID-7).

    The date is kept as written, at year, month or day precision, and an
    offset never moves it. With minutes or finer, ``time`` is the wall time
    as written in its own offset, else in ``default_tz`` (the message's
    zone, as for every other timestamp), with seconds zero-filled when
    absent, since a FHIR dateTime needs them. It is not moved to UTC, so
    its date is always ``date``. An hour alone gives no time: FHIR has no
    hour precision and the minutes would be invented.

    Raises TimestampError (fixed text, never the value) for anything else:
    a fraction without seconds, more than four fraction digits, an
    out-of-range part, or, when there is a time, an offset FHIR cannot hold.
    """
    m = _BIRTH_RE.match(value.strip())
    if m is None:
        raise TimestampError("malformed HL7 date of birth")
    body, frac, off = m.group("body"), m.group("frac"), m.group("off")
    if frac is not None and len(body) != 14:
        raise TimestampError("an HL7 fraction of a second needs the seconds")
    if off is not None and (int(off[1:3]) > 23 or int(off[3:5]) > 59):
        raise TimestampError("invalid UTC offset in an HL7 date of birth")
    if len(body) < 8:
        year = int(body[0:4])
        if year < 1 or (len(body) == 6 and not 1 <= int(body[4:6]) <= 12):
            raise TimestampError("out-of-range HL7 date of birth")
        return BirthValue(body[0:4] if len(body) == 4 else f"{body[0:4]}-{body[4:6]}", None)
    try:
        dt = parse_hl7_ts(value, default_tz=default_tz)
    except TimestampError:
        raise TimestampError("out-of-range HL7 date of birth") from None
    date = dt.date().isoformat()
    if len(body) < 12:
        return BirthValue(date, None)
    offset = dt.utcoffset()
    if offset is None or offset.total_seconds() % 60 or abs(offset) > _MAX_FHIR_OFFSET:
        raise TimestampError("the date of birth has a UTC offset FHIR cannot hold")
    minutes = round(offset.total_seconds() / 60)
    sign = "+" if minutes >= 0 else "-"
    zone = f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    fraction = f".{frac}" if frac else ""
    return BirthValue(date, f"{date}T{dt:%H:%M:%S}{fraction}{zone}")


def is_date_only(value: str) -> bool:
    """True for a valid HL7 timestamp that holds a date and no time
    (``YYYYMMDD``, with or without an offset)."""
    m = _TS_RE.match(value.strip())
    return m is not None and len(m.group("body")) == 8 and m.group("frac") is None


def message_tz(msh7: str, fallback: tzinfo) -> tzinfo:
    """The default time zone for a message's times. HL7 v2 makes the UTC
    offset of MSH-7 the default for every time in the message that carries
    none, so that offset wins over the hospital's configured zone. An
    MSH-7 with no offset, or one that does not parse, leaves ``fallback``."""
    m = _TS_RE.match(msh7.strip())
    if m is None or m.group("off") is None:
        return fallback
    try:
        parsed = parse_hl7_ts(msh7, default_tz=fallback)
    except TimestampError:
        return fallback
    return parsed.tzinfo or fallback


def format_hl7_ts(dt: datetime, *, include_offset: bool) -> str:
    """Format an aware datetime as HL7 DTM (second precision).

    Naive datetimes are a programming error and are rejected loudly.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise TimestampError("refusing to format a naive datetime as HL7 DTM")
    body = dt.strftime("%Y%m%d%H%M%S")
    if not include_offset:
        return body
    offset = dt.utcoffset()
    assert offset is not None  # checked above
    total_minutes = round(offset.total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{body}{sign}{total_minutes // 60:02d}{total_minutes % 60:02d}"
