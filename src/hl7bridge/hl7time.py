"""HL7 DTM timestamp parsing and formatting.

Policy (engineering rule 4):
- Every parsed timestamp is timezone-aware. If the value carries an explicit
  ``+HHMM`` / ``-HHMM`` offset, that offset wins. Otherwise the hospital's
  configured timezone is attached (this is per-hospital config, never a guess).
- Minimum accepted precision is a full date (``YYYYMMDD``). Anything shorter,
  longer, or non-numeric raises :class:`TimestampError`. Nothing is coerced.
- Local times that fall inside a DST transition resolve per PEP 495 with
  ``fold=0`` (the first occurrence of an ambiguous wall time, and the standard
  mapping for nonexistent spring-forward times). Deterministic and documented,
  not silent: callers that care can compare offsets.
"""

from __future__ import annotations

import re
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
