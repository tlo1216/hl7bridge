"""Fail if internal planning references or tool names appear in the repo.

Usage:
    python tools/check_references.py            # scan the working tree
    python tools/check_references.py --commits  # also scan commit messages

The patterns live only here. Each one is written with a character class
(for example ``[x]yz``) so this file never matches its own patterns, and the
scan skips this file's path as well. ``tests/unit/test_check_references.py``
plants every word in a temporary tree to prove the check is not blind, and
scans this file's own text to prove it does not match itself.

ALLOW holds exact (path, pattern name) pairs that are allowed to match. It
starts empty. Add a pair only with a reason next to it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "planning-note numbering": re.compile(r"\b[p]rojects? [0-9]", re.IGNORECASE),
    "planning-note rule numbering": re.compile(r"\b[e]ngineering rules?\b", re.IGNORECASE),
    "planning brief": re.compile(r"\b[p]roject brief\b", re.IGNORECASE),
    "client name": re.compile(r"\b[s]iim\b", re.IGNORECASE),
    "assistant name": re.compile(r"\b[c]laude\b", re.IGNORECASE),
    "assistant vendor": re.compile(r"\b[a]nthropic\b", re.IGNORECASE),
    "review tool name": re.compile(r"\b[c]odex\b", re.IGNORECASE),
    "review agent name": re.compile(r"\b[f]able\b", re.IGNORECASE),
    "agent wording": re.compile(r"\b[s]ub-?agents?\b", re.IGNORECASE),
    "attribution trailer": re.compile(r"\b[c]o-authored-by\b", re.IGNORECASE),
}

# (repo-relative POSIX path, pattern name). Keep it short and justified.
ALLOW: frozenset[tuple[str, str]] = frozenset()

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    "data",
}
SELF = Path(__file__).resolve()


@dataclass(frozen=True, slots=True)
class Hit:
    where: str
    line: int
    pattern: str
    text: str

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.pattern}: {self.text.strip()[:120]}"


def scan_text(text: str, where: str) -> Iterator[Hit]:
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS.items():
            if pattern.search(line) and (where, name) not in ALLOW:
                yield Hit(where, lineno, name, line)


def _git_files(root: Path) -> list[Path] | None:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [root / p for p in out.decode("utf-8").split("\0") if p]


def _walk(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts):
            yield path


def iter_files(root: Path) -> Iterable[Path]:
    files = _git_files(root) if (root / ".git").exists() else None
    return files if files is not None else _walk(root)


def scan_tree(root: Path) -> list[Hit]:
    hits: list[Hit] = []
    for path in sorted(iter_files(root)):
        if not path.is_file() or path.resolve() == SELF:
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        rel = path.relative_to(root).as_posix()
        hits.extend(scan_text(data.decode("utf-8", errors="replace"), rel))
    return hits


def scan_commits(root: Path) -> list[Hit]:
    out = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x01"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")
    hits: list[Hit] = []
    for record in out.split("\x01"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, body = record.partition("\0")
        hits.extend(scan_text(body, f"commit {sha[:12]}"))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--commits", action="store_true", help="also scan commit messages")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    hits = scan_tree(root)
    if args.commits:
        hits.extend(scan_commits(root))
    for hit in hits:
        print(hit)
    if hits:
        print(f"reference check failed: {len(hits)} hit(s)", file=sys.stderr)
        return 1
    print("reference check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
