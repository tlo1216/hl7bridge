"""Fail on any published container port that is not bound to 127.0.0.1.

Reads every compose file in the repository (``compose*.y*ml`` and
``docker-compose*.y*ml``) and every ``docker run`` line in the text files
Git tracks. A published port must name ``127.0.0.1`` as its host address.
``network_mode: host`` and ``docker run -P`` (publish everything) fail too.

Usage: python tools/check_ports.py [--root PATH]
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LOOPBACK = "127.0.0.1"
COMPOSE_NAME = re.compile(r"^(docker-)?compose([.\w-]*)?\.ya?ml$")
TEXT_SUFFIXES = {".md", ".sh", ".yml", ".yaml", ".txt", ".ps1", ".bat", ".cmd", ""}
DOCKER_RUN = re.compile(r"\bdocker\s+(?:container\s+)?run\b")


@dataclass(frozen=True, slots=True)
class Problem:
    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"


def host_of_short_port(spec: str) -> str | None:
    """The host address of a short port spec, or None when it has none.

    ``"2575"`` and ``"2575:2575"`` have no host address (every interface).
    ``"127.0.0.1:2575:2575"`` and ``"[::1]:2575:2575"`` do."""
    spec = spec.split("/", 1)[0]  # drop /tcp, /udp
    if spec.startswith("["):
        end = spec.find("]")
        return spec[1:end] if end != -1 and spec[end + 1 : end + 2] == ":" else None
    parts = spec.split(":")
    return parts[0] if len(parts) == 3 else None


def check_compose(data: Any, where: str) -> list[Problem]:
    problems: list[Problem] = []
    if not isinstance(data, dict):
        return problems
    services = data.get("services") or {}
    if not isinstance(services, dict):
        return [Problem(where, "services is not a mapping")]
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        if str(service.get("network_mode", "")).strip() == "host":
            problems.append(Problem(f"{where} {name}", "network_mode: host publishes every port"))
        for entry in service.get("ports") or []:
            if isinstance(entry, dict):
                published = entry.get("published")
                host = entry.get("host_ip")
                if published is not None and host != LOOPBACK:
                    problems.append(
                        Problem(f"{where} {name}", f"port {published} has host_ip {host!r}")
                    )
                continue
            spec = str(entry)
            host = host_of_short_port(spec)
            if host != LOOPBACK:
                problems.append(
                    Problem(f"{where} {name}", f"port {spec!r} is not bound to {LOOPBACK}")
                )
    return problems


def check_docker_run_line(line: str, where: str) -> list[Problem]:
    """Check the -p / --publish / -P options of one ``docker run`` line."""
    match = DOCKER_RUN.search(line)
    if not match:
        return []
    try:
        words = shlex.split(line[match.end() :], comments=True)
    except ValueError:
        words = line[match.end() :].split()
    problems: list[Problem] = []
    i = 0
    while i < len(words):
        word = words[i]
        spec: str | None = None
        if word in ("-P", "--publish-all"):
            problems.append(Problem(where, f"{word} publishes every exposed port"))
        elif word in ("-p", "--publish") and i + 1 < len(words):
            spec = words[i + 1]
            i += 1
        elif word.startswith("--publish="):
            spec = word.split("=", 1)[1]
        elif word.startswith("-p") and len(word) > 2 and not word.startswith("--"):
            spec = word[2:]
        if spec is not None and host_of_short_port(spec) != LOOPBACK:
            problems.append(Problem(where, f"docker run publishes {spec!r} beyond {LOOPBACK}"))
        i += 1
    return problems


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
    return sorted(root / name for name in out.decode("utf-8").split("\0") if name)


def check_tree(root: Path) -> list[Problem]:
    problems: list[Problem] = []
    for path in tracked_files(root):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if COMPOSE_NAME.match(path.name):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                problems.append(Problem(rel, f"not valid YAML ({type(exc).__name__})"))
                continue
            problems.extend(check_compose(data, rel))
        if path.suffix not in TEXT_SUFFIXES or path.name == "check_ports.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # Join backslash continuations so a multi-line docker run is one line.
        joined = re.sub(r"\\\r?\n", " ", text)
        for n, line in enumerate(joined.splitlines(), start=1):
            problems.extend(check_docker_run_line(line, f"{rel}:{n}"))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    problems = check_tree(Path(args.root))
    for problem in problems:
        print(f"PORT EXPOSED: {problem}", file=sys.stderr)
    if problems:
        return 1
    print("check_ports: every published port is bound to 127.0.0.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
