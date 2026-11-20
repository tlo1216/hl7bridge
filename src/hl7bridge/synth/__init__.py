"""Synthetic HL7 + DICOM test data generator. No real PHI, ever."""

from .generator import generate
from .manifest import Manifest, load_manifest, verify_manifest

__all__ = ["Manifest", "generate", "load_manifest", "verify_manifest"]
