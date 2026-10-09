"""Write metadata-only DICOM part-10 files for synthetic studies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydicom import dcmwrite
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid


@dataclass(frozen=True, slots=True)
class StudySpec:
    hospital_id: str
    accession: str
    modality: str
    patient_mrn: str
    patient_family: str
    patient_given: str
    study_at: datetime  # aware
    include_offset_tag: bool
    uid_entropy: str


def study_uid(spec: StudySpec) -> str:
    return str(generate_uid(entropy_srcs=[spec.uid_entropy]))


def write_study(directory: Path, spec: StudySpec) -> Path:
    if spec.study_at.tzinfo is None or spec.study_at.utcoffset() is None:
        raise ValueError("StudySpec.study_at must be timezone-aware")

    uid = study_uid(spec)
    ds = Dataset()
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.PatientID = spec.patient_mrn
    ds.PatientName = f"{spec.patient_family}^{spec.patient_given}"
    ds.AccessionNumber = spec.accession
    ds.StudyInstanceUID = uid
    ds.SOPInstanceUID = generate_uid(entropy_srcs=[spec.uid_entropy + ":sop"])
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.Modality = spec.modality
    ds.StudyDate = spec.study_at.strftime("%Y%m%d")
    ds.StudyTime = spec.study_at.strftime("%H%M%S")
    if spec.include_offset_tag:
        offset = spec.study_at.utcoffset()
        assert offset is not None  # checked above
        total = round(offset.total_seconds() / 60)
        sign = "+" if total >= 0 else "-"
        ds.TimezoneOffsetFromUTC = f"{sign}{abs(total) // 60:02d}{abs(total) % 60:02d}"

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = meta

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uid}.dcm"
    dcmwrite(path, ds, enforce_file_format=True)
    return path
