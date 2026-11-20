"""Check the licenses of hl7bridge's runtime dependencies against an allow list.

Walks the installed dependency tree of a distribution (``hl7bridge`` by
default), following only requirements whose environment markers hold here
and skipping extras, then reads each package's license from its metadata:
``License-Expression`` when present, otherwise a short ``License`` field
and every ``License ::`` trove classifier together. A package fails the
check when any license it names is not on the allow list, or when none
can be read. Markers are evaluated on the machine running the check, so
CI runs it on every OS in the test matrix (tzdata is a Windows-only
dependency).

Usage: python tools/check_licenses.py [--dist NAME]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# Permissive licenses, plus MPL-2.0 (file-level copyleft; fine for a
# dependency used unmodified, which is how certifi is used).
ALLOWED = frozenset(
    {
        "MIT",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BSD",
        "Apache-2.0",
        "PSF-2.0",
        "ISC",
        "MPL-2.0",
    }
)

CLASSIFIERS = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: BSD License": "BSD",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}

# Short free-text License fields seen in the wild, mapped to an id.
FREE_TEXT = {
    "mit": "MIT",
    "mit license": "MIT",
    "bsd": "BSD",
    "bsd license": "BSD",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "mpl-2.0": "MPL-2.0",
    "psf-2.0": "PSF-2.0",
    "isc": "ISC",
}


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    version: str
    license: str | None

    @property
    def allowed(self) -> bool:
        return self.license is not None and all(
            part in ALLOWED for part in split_expression(self.license)
        )


def split_expression(expression: str) -> list[str]:
    """The license ids in an SPDX expression. ``MIT OR Apache-2.0`` gives
    both; every one must be allowed, which is stricter than SPDX needs."""
    return [p.strip() for p in re.split(r"\s+(?:AND|OR|WITH)\s+|[()]", expression) if p.strip()]


def license_of(meta: metadata.PackageMetadata) -> str | None:
    """The license of one package, as an expression of ids.

    ``License-Expression`` wins when present. Otherwise every other source
    counts, and every one must be allowed: a short ``License`` field (mapped
    to an id, or kept as written so an unknown one fails) and every
    ``License ::`` classifier (one not in the table is kept as written, so
    it fails too). A long ``License`` field is the license text itself and
    is skipped; the classifiers then decide."""
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    found: set[str] = set()
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) < 60 and text.upper() != "UNKNOWN":
        found.add(FREE_TEXT.get(text.lower(), text))
    for classifier in meta.get_all("Classifier") or []:
        if not classifier.startswith("License ::") or classifier == "License :: OSI Approved":
            continue
        found.add(CLASSIFIERS.get(classifier, classifier.rsplit("::", 1)[-1].strip()))
    if not found:
        return None
    return " AND ".join(f"({part})" if " " in part else part for part in sorted(found))


def walk(
    root: str,
    get: Callable[[str], metadata.Distribution] = metadata.distribution,
) -> list[Result]:
    """Every runtime dependency of ``root``, not ``root`` itself."""
    seen: dict[str, Result] = {}
    queue: list[str] = [root]
    root_key = canonicalize_name(root)
    while queue:
        name = queue.pop()
        key = canonicalize_name(name)
        if key in seen or (key == root_key and seen):
            continue
        dist = get(name)
        if key != root_key:
            seen[key] = Result(dist.metadata["Name"], dist.version, license_of(dist.metadata))
        else:
            seen[key] = Result(name, dist.version, "root")
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None and not req.marker.evaluate({"extra": ""}):
                continue
            queue.append(req.name)
    return sorted((r for k, r in seen.items() if k != root_key), key=lambda r: r.name.lower())


def render(results: Iterable[Result]) -> list[str]:
    lines = []
    for r in results:
        verdict = "ok" if r.allowed else "NOT ALLOWED"
        lines.append(f"{r.name} {r.version}: {r.license or 'unknown'} [{verdict}]")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", default="hl7bridge")
    args = parser.parse_args(argv)
    results = walk(args.dist)
    for line in render(results):
        print(line)
    bad = [r for r in results if not r.allowed]
    if bad:
        print(f"check_licenses: {len(bad)} package(s) not on the allow list", file=sys.stderr)
        return 1
    print(f"check_licenses: {len(results)} runtime dependencies, all on the allow list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
