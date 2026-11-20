"""Fail if words this repository keeps out of its text appear in it.

Usage:
    python tools/check_references.py            # scan the working tree
    python tools/check_references.py --commits  # also scan commit messages

The words are not written here. Each line is split into lower-case word
tokens (runs of letters and digits; a token of digits only counts as "#"),
and every run of one to three tokens is hashed with a fixed salt. A rule
fails the scan when one of its digests turns up. Rule ids are neutral, so
reading this file tells you neither the words nor what they are about. The
digests cannot match themselves, and the scan skips this file's path too.

``tests/unit/test_check_references.py`` proves the tokenizer and the
matching on stand-in words, and, when ``HL7BRIDGE_REFCHECK_SAMPLES`` names a
local file of real samples (kept outside the repository), that every rule
catches its sample.

ALLOW holds exact (path, rule id) pairs that are allowed to match. It
starts empty. Add a pair only with a reason next to it.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

SALT = b"hl7bridge-reference-check\x00"
MAX_NGRAM = 3
DIGEST_CHARS = 16

# rule id -> digests of the token runs it rejects.
RULES: dict[str, frozenset[str]] = {
    "R01": frozenset({"35f29f46cfbea24d", "7dd91001ff8d3dbd"}),
    "R02": frozenset({"d37cd106b22c8e00", "f59537556fb7851e"}),
    "R03": frozenset({"4a0ca41de919eb66"}),
    "R04": frozenset({"e34563ee66975f97"}),
    "R05": frozenset({"590ecabe5ce863ff"}),
    "R06": frozenset({"b58132f515028e0a"}),
    "R07": frozenset({"cb3df1874cca52b5"}),
    "R08": frozenset({"6aea47668e5be577"}),
    "R09": frozenset(
        {"12bff9bfd1cf1618", "5ca4c1568998f99e", "96911d9786816c0c", "c4265c827409e6e5"}
    ),
    "R10": frozenset({"520516eb522d58c3"}),
}

# (repo-relative POSIX path, rule id). Keep it short and justified.
ALLOW: frozenset[tuple[str, str]] = frozenset()

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def digest(ngram: str) -> str:
    """Salted digest of one space-joined token run."""
    return hashlib.sha256(SALT + ngram.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def tokens(line: str) -> list[str]:
    return ["#" if t.isdigit() else t for t in _TOKEN_RE.findall(line.lower())]


def line_digests(line: str) -> set[str]:
    toks = tokens(line)
    found: set[str] = set()
    for size in range(1, MAX_NGRAM + 1):
        for i in range(len(toks) - size + 1):
            found.add(digest(" ".join(toks[i : i + size])))
    return found


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
        found = line_digests(line)
        for rule, digests in RULES.items():
            if found & digests and (where, rule) not in ALLOW:
                yield Hit(where, lineno, rule, line)


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
