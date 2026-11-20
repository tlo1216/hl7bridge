"""Finding the message files a command was pointed at."""

from __future__ import annotations

from pathlib import Path

from .errors import UsageError


def hl7_files(path: Path) -> list[Path]:
    """``path`` itself when it is a file, else every ``*.hl7`` below it,
    sorted. A missing path, or a folder with no ``*.hl7`` files, raises
    UsageError with a hint."""
    if path.is_file():
        return [path]
    if not path.exists():
        raise UsageError(f"{path} does not exist.", hint="Try: hl7bridge init")
    paths = sorted(path.rglob("*.hl7"))
    if not paths:
        raise UsageError(f"no *.hl7 files under {path}.", hint="Try: hl7bridge init")
    return paths


def display_key(path: Path, root: Path) -> str:
    """The name a report uses for ``path``: relative to ``root`` with
    forward slashes on every OS, or the file name when ``root`` is the file."""
    if path == root:
        return path.name
    return path.relative_to(root).as_posix()
