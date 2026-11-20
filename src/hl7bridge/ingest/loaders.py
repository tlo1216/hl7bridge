"""Loaders for non-MLLP inputs: DICOM study metadata and the outbound
report log. Both fail loudly on anything they cannot interpret.

Error text names the file, the line or the field, never the value that was
rejected, and is raised ``from None`` so no chained exception carries the
value either. pydicom reports a value that breaks its VR rules through its
own logger and through ``warnings`` with the value quoted; the study loader
captures both channels while it reads a file and turns any report into a
StudyLoadError that names the file only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError

from ..config import HospitalRegistry
from ..errors import BridgeError, TimestampError
from ..hl7time import parse_hl7_ts
from ..store import utc_iso
from ..store.repo import StudyRow, insert_study, record_outbound_report

log = logging.getLogger(__name__)


class StudyLoadError(BridgeError):
    """A DICOM file could not be loaded into the studies table."""


@dataclass(slots=True)
class _Captured:
    """What pydicom tried to report while one file was read: counts and
    categories only, never the text."""

    log_records: int = 0
    warning_types: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.log_records or self.warning_types)


class _DropAndCount(logging.Filter):
    def __init__(self, captured: _Captured) -> None:
        super().__init__()
        self.captured = captured

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            self.captured.log_records += 1
        return False


@contextmanager
def _pydicom_diagnostics_captured() -> Iterator[_Captured]:
    """Hold back every pydicom log record and every warning raised while the
    block runs, and report what was held back.

    Not thread safe: ``warnings.catch_warnings`` swaps process-wide state.
    The CLI reads one file at a time on one thread.
    """
    captured = _Captured()
    pydicom_logger = logging.getLogger("pydicom")
    drop = _DropAndCount(captured)
    pydicom_logger.addFilter(drop)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            yield captured
        for item in caught:
            captured.warning_types.add(item.category.__name__)
    finally:
        pydicom_logger.removeFilter(drop)


@dataclass(frozen=True, slots=True)
class _StudyFields:
    study_uid: str
    modality: str
    study_date: str
    study_time: str
    offset: str
    accession: str | None
    mrn: str | None


def _read_study_fields(path: Path) -> _StudyFields:
    """Read the header and every element the loader uses, so pydicom runs
    its value checks inside the capture."""
    ds: Dataset = dcmread(path, stop_before_pixels=True)
    return _StudyFields(
        study_uid=str(getattr(ds, "StudyInstanceUID", "") or ""),
        modality=str(getattr(ds, "Modality", "") or ""),
        study_date=str(getattr(ds, "StudyDate", "") or ""),
        study_time=str(getattr(ds, "StudyTime", "") or "").split(".")[0],
        offset=str(getattr(ds, "TimezoneOffsetFromUTC", "") or ""),
        accession=str(getattr(ds, "AccessionNumber", "") or "").strip() or None,
        mrn=str(getattr(ds, "PatientID", "") or "").strip() or None,
    )


def load_studies(
    conn: sqlite3.Connection, registry: HospitalRegistry, dicom_root: Path
) -> dict[str, int]:
    """Load <dicom_root>/<hospital_id>/*.dcm into the studies table.

    Returns {'inserted': n, 'replaced': n}. Raises StudyLoadError on the
    first file that cannot be interpreted; partially loaded batches are
    visible in the counts, never silent.
    """
    counts = {"inserted": 0, "replaced": 0}
    paths = sorted(dicom_root.glob("*/*.dcm"))
    if not paths:
        raise StudyLoadError(f"no */*.dcm files under {dicom_root}")
    for position, path in enumerate(paths, start=1):
        hospital = registry.by_id.get(path.parent.name)
        if hospital is None:
            raise StudyLoadError(f"{path.parent}: folder name is not a configured hospital_id")
        # The file name is never printed: files are often named by study UID,
        # which is an identifier. Error text gives the hospital folder and the
        # file's position in the sorted listing instead.
        where = f"{hospital.hospital_id} file #{position}"
        failure = ""
        with _pydicom_diagnostics_captured() as captured:
            try:
                fields = _read_study_fields(path)
            except InvalidDicomError:
                failure = "not a DICOM file"
            except (ValueError, TypeError, KeyError, EOFError, OverflowError) as exc:
                failure = f"a DICOM value could not be read ({type(exc).__name__})"
        if failure:
            raise StudyLoadError(f"{where}: {failure}")
        if captured:
            raise StudyLoadError(
                f"{where}: pydicom flagged an element value as invalid; the value is not shown"
            )

        if not fields.study_uid:
            raise StudyLoadError(f"{where}: missing StudyInstanceUID")
        if not fields.modality:
            raise StudyLoadError(f"{where}: missing Modality")
        try:
            study_at = parse_hl7_ts(
                f"{fields.study_date}{fields.study_time}{fields.offset}",
                default_tz=hospital.tzinfo,
            )
        except TimestampError:
            raise StudyLoadError(
                f"{where}: StudyDate, StudyTime or TimezoneOffsetFromUTC is not a valid timestamp"
            ) from None
        with conn:
            outcome = insert_study(
                conn,
                StudyRow(
                    study_uid=fields.study_uid,
                    hospital_id=hospital.hospital_id,
                    accession=fields.accession,
                    modality=fields.modality,
                    patient_mrn=fields.mrn,
                    study_at_utc=utc_iso(study_at),
                    source_file=path.name,
                ),
            )
        counts[outcome] += 1
    log.info(
        "loaded studies inserted=%d replaced=%d from %s",
        counts["inserted"],
        counts["replaced"],
        dicom_root,
    )
    return counts


def load_outbound(conn: sqlite3.Connection, registry: HospitalRegistry, path: Path) -> int:
    """Load the outbound report log (JSONL). Returns row count.

    Every entry must name a configured hospital; the ACK report could not
    judge its timeout otherwise.
    """
    required = {"hospital_id", "control_id", "accession", "sent_at_utc"}
    count = 0
    with conn:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                # The decoder's text names a position, never the content.
                raise BridgeError(f"{path}:{lineno}: invalid JSON at column {exc.colno}") from None
            if not isinstance(entry, dict):
                raise BridgeError(f"{path}:{lineno}: each line must be a JSON object")
            missing = required - set(entry)
            if missing:
                raise BridgeError(f"{path}:{lineno}: missing keys {sorted(missing)}")
            hospital_id = entry["hospital_id"]
            if not isinstance(hospital_id, str) or hospital_id not in registry.by_id:
                raise BridgeError(
                    f"{path}:{lineno}: hospital_id is not a configured hospital"
                    f" (configured: {', '.join(sorted(registry.by_id))})"
                )
            # The store's UTC invariant is enforced at this admission gate too:
            # an unvalidated value here would only explode later at read time.
            raw_sent = entry["sent_at_utc"]
            try:
                sent_at = datetime.fromisoformat(raw_sent)
            except (TypeError, ValueError):
                # The value is not echoed, here or in a chained cause.
                raise BridgeError(
                    f"{path}:{lineno}: sent_at_utc is not an ISO 8601 timestamp"
                ) from None
            if sent_at.tzinfo is None or sent_at.utcoffset() is None:
                raise BridgeError(
                    f"{path}:{lineno}: sent_at_utc is naive; it must carry a UTC offset"
                )
            record_outbound_report(
                conn,
                hospital_id=entry["hospital_id"],
                control_id=entry["control_id"],
                accession=entry["accession"],
                sent_at_utc=utc_iso(sent_at),
            )
            count += 1
    if count == 0:
        raise BridgeError(f"{path}: no outbound report entries loaded")
    log.info("loaded %d outbound report entries from %s", count, path)
    return count
