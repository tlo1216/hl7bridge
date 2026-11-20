"""Fail on any published container port that is not bound to 127.0.0.1.

Reads every YAML file Git tracks. A file with a top-level ``services``
mapping is a compose file, whatever its name (``compose.demo.yaml``,
``stack.yml``, a file pulled in with ``include``). A workflow file under
``.github/workflows`` is checked for the ports of its service and job
containers. Every other text file is searched for container commands:
``docker run``, ``docker create``, ``docker container run|create``,
``podman run|create``, and ``docker compose run`` / ``docker-compose run``.

A published port must name ``127.0.0.1`` as its host address. These fail:

- a short port with no host address (``"2575"``, ``"2575:2575"``);
- a long-form compose port whose ``host_ip`` is not 127.0.0.1, with or
  without ``published`` (without it Docker picks a host port on every
  interface);
- ``network_mode: host``, ``--network host``, ``--net=host``;
- ``-P`` / ``--publish-all``, alone or in a cluster such as ``-dP``;
- ``-p``, ``--publish`` and clusters ending in ``p`` (``-dp 8080:8080``,
  ``-itp8080:8080``) without the loopback address.

``docker compose run --service-ports`` publishes the ports in the compose
file, which this check reads too.

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
YAML_SUFFIXES = {".yml", ".yaml"}
TEXT_SUFFIXES = {".md", ".sh", ".yml", ".yaml", ".txt", ".ps1", ".bat", ".cmd", ""}
CONTAINER_COMMAND = re.compile(
    r"\b(?:(?:docker|podman)\s+(?:container\s+)?(?:run|create)"
    r"|(?:docker\s+compose|docker-compose|podman-compose)\s+run)\b"
)
# Short flags of docker run and create that take no value, so they can
# come before -p or -P in a cluster such as -dit or -dp.
BOOLEAN_SHORT_FLAGS = frozenset("dit")


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


def _check_port_entry(entry: Any, where: str) -> list[Problem]:
    if isinstance(entry, dict):
        host = entry.get("host_ip")
        if host != LOOPBACK:
            shown = entry.get("published", entry.get("target", "?"))
            return [Problem(where, f"port {shown} has host_ip {host!r}, not {LOOPBACK}")]
        return []
    spec = str(entry)
    if host_of_short_port(spec) != LOOPBACK:
        return [Problem(where, f"port {spec!r} is not bound to {LOOPBACK}")]
    return []


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
            problems.extend(_check_port_entry(entry, f"{where} {name}"))
    return problems


def check_workflow(data: Any, where: str) -> list[Problem]:
    """Ports of GitHub Actions service containers and job containers."""
    problems: list[Problem] = []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return problems
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        containers: list[tuple[str, Any]] = []
        services = job.get("services") or {}
        if isinstance(services, dict):
            containers += [(f"services.{n}", s) for n, s in services.items()]
        if isinstance(job.get("container"), dict):
            containers.append(("container", job["container"]))
        for label, container in containers:
            if not isinstance(container, dict):
                continue
            for entry in container.get("ports") or []:
                problems.extend(_check_port_entry(entry, f"{where} jobs.{job_name}.{label}"))
            options = str(container.get("options") or "")
            problems.extend(_check_words(_split(options), f"{where} jobs.{job_name}.{label}"))
    return problems


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text, comments=True)
    except ValueError:
        return text.split()


def _check_words(words: list[str], where: str) -> list[Problem]:
    """Publish and network options in the words of one container command."""
    problems: list[Problem] = []
    i = 0
    while i < len(words):
        word = words[i]
        specs: list[str] = []
        if word in ("-P", "--publish-all"):
            problems.append(Problem(where, f"{word} publishes every exposed port"))
        elif word in ("-p", "--publish") and i + 1 < len(words):
            specs.append(words[i + 1])
            i += 1
        elif word.startswith("--publish="):
            specs.append(word.split("=", 1)[1])
        elif word in ("--network", "--net") and i + 1 < len(words):
            if words[i + 1] == "host":
                problems.append(Problem(where, f"{word} host publishes every port"))
            i += 1
        elif word in ("--network=host", "--net=host"):
            problems.append(Problem(where, f"{word} publishes every port"))
        elif word.startswith("-") and not word.startswith("--") and len(word) > 1:
            # A cluster of short flags: -dp, -itp 8080:8080, -dp8080:8080, -dP.
            for j, flag in enumerate(word[1:], start=1):
                if flag == "P":
                    problems.append(Problem(where, f"{word} publishes every exposed port"))
                    break
                if flag == "p":
                    rest = word[j + 1 :]
                    if rest:
                        specs.append(rest)
                    elif i + 1 < len(words):
                        specs.append(words[i + 1])
                        i += 1
                    break
                if flag not in BOOLEAN_SHORT_FLAGS:
                    break  # a flag that takes a value; the rest is that value
        for spec in specs:
            if host_of_short_port(spec) != LOOPBACK:
                problems.append(Problem(where, f"publishes {spec!r} beyond {LOOPBACK}"))
        i += 1
    return problems


def check_docker_run_line(line: str, where: str) -> list[Problem]:
    """Check the publish and network options of one container command line."""
    match = CONTAINER_COMMAND.search(line)
    if not match:
        return []
    return _check_words(_split(line[match.end() :]), where)


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
    return sorted(root / name for name in out.decode("utf-8").split("\0") if name)


def _is_workflow(rel: str) -> bool:
    return rel.startswith(".github/workflows/")


def check_tree(root: Path) -> list[Problem]:
    problems: list[Problem] = []
    for path in tracked_files(root):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() in YAML_SUFFIXES:
            try:
                documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            except (yaml.YAMLError, UnicodeDecodeError) as exc:
                problems.append(Problem(rel, f"not valid YAML ({type(exc).__name__})"))
                continue
            for data in documents:
                if _is_workflow(rel):
                    problems.extend(check_workflow(data, rel))
                elif isinstance(data, dict) and isinstance(data.get("services"), dict):
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
