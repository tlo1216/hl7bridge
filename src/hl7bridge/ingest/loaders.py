"""Loaders for non-MLLP inputs: DICOM study metadata and the outbound
report log. Both fail loudly on anything they cannot interpret."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from pydicom import dcmread
from pydicom.errors import InvalidDicomError

from ..config import HospitalRegistry
from ..errors import BridgeError, TimestampError
from ..hl7time import parse_hl7_ts
from ..store import utc_iso
from ..store.repo import StudyRow, insert_study, record_outbound_report

log = logging.getLogger(__name__)


class StudyLoadError(BridgeError):
    """A DICOM file could not be loaded into the studies table."""


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
    for path in paths:
        hospital = registry.by_id.get(path.parent.name)
        if hospital is None:
            raise StudyLoadError(
                f"{path}: directory {path.parent.name!r} is not a configured hospital_id"
            )
        try:
            ds = dcmread(path, stop_before_pixels=True)
        except InvalidDicomError as exc:
            raise StudyLoadError(f"{path}: not a DICOM file: {exc}") from exc

        study_uid = str(getattr(ds, "StudyInstanceUID", "") or "")
        if not study_uid:
            raise StudyLoadError(f"{path}: missing StudyInstanceUID")
        modality = str(getattr(ds, "Modality", "") or "")
        if not modality:
            raise StudyLoadError(f"{path}: missing Modality")
        study_date = str(getattr(ds, "StudyDate", "") or "")
        study_time = str(getattr(ds, "StudyTime", "") or "").split(".")[0]
        offset = str(getattr(ds, "TimezoneOffsetFromUTC", "") or "")
        try:
            study_at = parse_hl7_ts(f"{study_date}{study_time}{offset}", default_tz=hospital.tzinfo)
        except TimestampError as exc:
            raise StudyLoadError(f"{path}: bad StudyDate/StudyTime: {exc}") from exc

        accession = str(getattr(ds, "AccessionNumber", "") or "").strip() or None
        mrn = str(getattr(ds, "PatientID", "") or "").strip() or None
        with conn:
            outcome = insert_study(
                conn,
                StudyRow(
                    study_uid=study_uid,
                    hospital_id=hospital.hospital_id,
                    accession=accession,
                    modality=modality,
                    patient_mrn=mrn,
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


def load_outbound(conn: sqlite3.Connection, path: Path) -> int:
    """Load the outbound report log (JSONL). Returns row count."""
    required = {"hospital_id", "control_id", "accession", "sent_at_utc"}
    count = 0
    with conn:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BridgeError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            missing = required - set(entry)
            if missing:
                raise BridgeError(f"{path}:{lineno}: missing keys {sorted(missing)}")
            # The store's UTC invariant is enforced at this admission gate too:
            # an unvalidated value here would only explode later at read time.
            raw_sent = entry["sent_at_utc"]
            try:
                sent_at = datetime.fromisoformat(raw_sent)
            except (TypeError, ValueError) as exc:
                raise BridgeError(f"{path}:{lineno}: bad sent_at_utc {raw_sent!r}: {exc}") from exc
            if sent_at.tzinfo is None or sent_at.utcoffset() is None:
                raise BridgeError(
                    f"{path}:{lineno}: sent_at_utc {raw_sent!r} is naive; must carry a UTC offset"
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
