"""Where the generator writes its files.

``generate`` writes every folder and file through a sink. ``PlainSink``
writes to disk as ``synth --out`` always has: folders are made as needed
and files are replaced. ``hl7bridge demo`` passes its own sink, which
creates each file and folder exclusively and records it, so the demo can
later remove exactly what it wrote (see ``hl7bridge.demo``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class FileSink(Protocol):
    def makedirs(self, path: Path) -> None:
        """Make ``path`` and any missing parents."""

    def write_bytes(self, path: Path, data: bytes) -> None:
        """Write one file. Its folder already exists."""


class PlainSink:
    def makedirs(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def write_bytes(self, path: Path, data: bytes) -> None:
        path.write_bytes(data)
