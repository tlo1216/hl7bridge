"""The README's quickstart runs as written, and every hl7bridge command the
README and docs/cli.md show parses with the real parser.

A fenced block right after a ``<!-- readme-test -->`` line is run, line by
line, through ``cli.main`` in one fresh folder, in README order. A command
runs as the user would type it, with no extra options."""

from __future__ import annotations

import logging
import re
import shlex
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from hl7bridge import cli
from hl7bridge.logsafety import QUIET_LOGGERS

REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text(encoding="utf-8")
CLI_DOC = (REPO / "docs" / "cli.md").read_text(encoding="utf-8")

# Commands that exit 1 on purpose in the README walk-through.
EXPECTED_STATUS = {"hl7bridge lint hl7bridge/samples": 1}


def _tested_commands(text: str) -> list[str]:
    commands: list[str] = []
    for block in re.findall(r"<!-- readme-test -->\n```[a-z]*\n(.*?)```", text, flags=re.DOTALL):
        commands.extend(line.strip() for line in block.splitlines() if line.strip())
    return commands


def _all_commands(text: str) -> list[str]:
    """``hl7bridge ...`` lines in fenced blocks, and in backticks."""
    found: list[str] = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.DOTALL):
        found.extend(
            line.strip() for line in block.splitlines() if line.strip().startswith("hl7bridge ")
        )
    found.extend(re.findall(r"`(hl7bridge [^`]+)`", text))
    # Lines with a <placeholder> are templates, not command lines.
    return [c for c in found if "<" not in c]


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    saved = {n: logging.getLogger(n).level for n in (*QUIET_LOGGERS, "hl7bridge")}
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_readme_marks_the_quickstart_blocks():
    commands = _tested_commands(README)
    assert "hl7bridge demo" in commands
    assert "hl7bridge init" in commands
    assert any(c.startswith("hl7bridge synth") for c in commands)


def test_readme_quickstart_runs_as_written(tmp_path, monkeypatch, capsys):
    for name in ("HL7BRIDGE_CONFIG", "HL7BRIDGE_DB", "HL7BRIDGE_FHIR_BASE"):
        monkeypatch.delenv(name, raising=False)
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    for command in _tested_commands(README):
        argv = shlex.split(command)
        assert argv[0] == "hl7bridge"
        status = cli.main(argv[1:])
        captured = capsys.readouterr()
        want = EXPECTED_STATUS.get(command, 0)
        assert status == want, f"{command!r} exited {status}:\n{captured.err[-2000:]}"
    assert list(temp_root.iterdir()) == [], "the demo removed its temporary folder"


@pytest.mark.parametrize("command", sorted(set(_all_commands(README) + _all_commands(CLI_DOC))))
def test_documented_commands_parse(command):
    argv = shlex.split(command.replace("\\", "/"))[1:]
    parser = cli.build_parser()
    if argv in (["--version"], ["--help"]):
        return
    if " ".join(argv) in {c.name for c in cli.COMMANDS}:
        return  # a bare command name in prose, such as `hl7bridge synth`
    try:
        parser.parse_args(argv)
    except SystemExit as exc:  # pragma: no cover - the failure message is the point
        raise AssertionError(f"documented command does not parse: {command!r}") from exc
