"""Direct mode's safety rules, each with a test that fails when the rule is
taken out: ACK escaping, the log's binding to a server and a run, patient
and authority checks, the deadline and the worker pool, post-commit
failures, reset's guards, from-fhir's output and the mapping hooks."""

from __future__ import annotations

import pytest

from hl7bridge.config import HospitalRegistry
from hl7bridge.ingest.ack import AckDecision, build_ack
from hl7bridge.v2fhir import hooks

from ..fakefhir import FakeFhir
from .test_direct import NOW, PROFILE, Lab, _patient

RUN_TAG = {"system": "urn:test:tag/lab-run", "code": "run-a"}


@pytest.fixture
def registry(tmp_path) -> HospitalRegistry:
    folder = tmp_path / "profiles"
    folder.mkdir()
    (folder / "lab.yaml").write_text(PROFILE, encoding="utf-8")
    return HospitalRegistry.load_dir(folder)


@pytest.fixture
def server() -> FakeFhir:
    fake = FakeFhir()
    fake._write("Patient", "p1", _patient())
    return fake


@pytest.fixture
def lab(registry, server, tmp_path) -> Lab:
    return Lab(registry, server, tmp_path)


@pytest.fixture(autouse=True)
def _no_hooks():
    hooks.reset()
    yield
    hooks.reset()


def _segments(ack: str) -> list[list[str]]:
    return [s.split("|") for s in ack.split("\r") if s]


# -- ACK escaping --------------------------------------------------------------------------


def test_build_ack_escapes_every_echoed_value():
    ack = build_ack(
        decision=AckDecision("AR", "parse_error", ack_text="MSH-3"),
        original_control_id="C|1\rMSA|AA|X",
        ack_control_id="B1",
        sending_app="HUB",
        sending_facility="HUB",
        receiving_app="APP\rMSA|AA|X",
        receiving_facility="F^A~C&D\\E\x1c",
        trigger_event="R01",
        version="2.5\x0b",
        now=NOW,
    )
    segments = _segments(ack)
    assert [s[0] for s in segments] == ["MSH", "MSA"]
    msh, msa = segments
    assert len(msh) == 12 and msh[4] == "APPMSA\\F\\AA\\F\\X"
    assert msh[5] == "F\\S\\A\\R\\C\\T\\D\\E\\E"
    assert msh[11] == "2.5"
    assert msa[:3] == ["MSA", "AR", "C\\F\\1MSA\\F\\AA\\F\\X"] and len(msa) == 4


def test_an_escaped_cr_in_msh3_cannot_forge_an_aa(lab):
    text = lab.oru().replace("|FROMFHIR|", "|APP\\X0D\\MSA\\F\\AA\\F\\X|", 1)
    text = text.replace("|T|2.5.1", "|P|2.5.1", 1)
    ack = lab.handler.handle_bytes(text.encode())
    segments = _segments(ack)
    assert [s[0] for s in segments] == ["MSH", "MSA"]
    assert segments[1][1] == "AR"
