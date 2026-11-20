"""Static checks on the files that ship or build hl7bridge: the bundled
samples and profiles, docs/profiles.md, the Dockerfile and compose files,
the published-port check and the license check.

Docker itself is exercised in CI (the docker job). These tests run
everywhere and need no Docker."""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

import pytest
import yaml

from hl7bridge import __version__
from hl7bridge.config import (
    IDENTIFIER_KINDS,
    KNOWN_KEYS,
    KNOWN_LINT_KEYS,
    KNOWN_RECONCILE_KEYS,
    KNOWN_TAT_KEYS,
    HospitalRegistry,
)
from hl7bridge.ingest import IngestPipeline
from hl7bridge.ingest.parser import get_field, parse_hl7
from hl7bridge.lint import lint_directory
from hl7bridge.profiles import BUNDLED_PROFILES, SAMPLES_DIR
from hl7bridge.store import connect

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

import check_licenses  # noqa: E402
import check_ports  # noqa: E402

# -- samples and profiles -------------------------------------------------------------

EXPECTED_SAMPLES = {
    "01_hospa_adt_a04.hl7": ("AA", ""),
    "02_hospa_orm_o01.hl7": ("AA", ""),
    "03_hospa_oru_r01_final.hl7": ("AA", ""),
    "04_hospb_adt_a08.hl7": ("AA", ""),
    "05_hospb_orm_o01_zsegment.hl7": ("AA", ""),
    "06_hospb_oru_r01_prelim.hl7": ("AA", ""),
    "07_hospc_adt_a04_offsets.hl7": ("AA", ""),
    "08_hospc_adt_a40_merge.hl7": ("AA", ""),
    "09_hospc_orm_o01.hl7": ("AA", ""),
    "10_hospc_ack_r01.hl7": ("AA", ""),
    "11_broken_missing_accession.hl7": ("AE", "missing_accession: ORC-3"),
    "12_broken_bad_timestamp.hl7": ("AE", "bad_timestamp: OBR-22"),
    "13_hospc_adt_a01_inpatient.hl7": ("AA", ""),
    "14_hospa_oru_r01_structured.hl7": ("AA", ""),
    "15_hospc_adt_a08_discharge.hl7": ("AA", ""),
}


def test_samples_get_the_expected_acks(tmp_path):
    registry = HospitalRegistry.load_dir(BUNDLED_PROFILES)
    conn = connect(tmp_path / "s.db")
    pipeline = IngestPipeline(registry, conn)
    got = {}
    for path in sorted(SAMPLES_DIR.glob("*.hl7")):
        ack = parse_hl7(pipeline.handle_bytes(path.read_bytes()))
        got[path.name] = (get_field(ack, "MSA-1"), get_field(ack, "MSA-3") or "")
    conn.close()
    assert got == EXPECTED_SAMPLES


def test_samples_lint_findings_are_only_the_two_broken_ones():
    registry = HospitalRegistry.load_dir(BUNDLED_PROFILES)
    report = lint_directory(SAMPLES_DIR, registry)
    assert report.files_checked == 15
    assert {name: [f.rule_id for f in fs] for name, fs in report.by_file} == {
        "11_broken_missing_accession.hl7": ["HL7-010"],
        "12_broken_bad_timestamp.hl7": ["HL7-014"],
    }


def test_samples_are_synthetic_and_training_traffic():
    for path in SAMPLES_DIR.glob("*.hl7"):
        raw = path.read_bytes()
        assert b"\n" not in raw, f"{path.name}: segments end with a bare CR"
        message = parse_hl7(raw.decode("ascii"))
        assert get_field(message, "MSH-11") == "T", f"{path.name}: processing id is T"
        assert b"ZZZSAMPLE" in raw or b"MSA|" in raw


def test_every_profile_starts_with_a_comment_on_its_dialect():
    for path in BUNDLED_PROFILES.glob("*.yaml"):
        first = path.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("# "), path.name


def test_profiles_and_samples_are_inside_the_package():
    package = Path(__import__("hl7bridge").__file__).resolve().parent
    assert BUNDLED_PROFILES.parent == package
    assert SAMPLES_DIR.parent == package


def test_profiles_doc_covers_every_key():
    doc = (REPO / "docs" / "profiles.md").read_text(encoding="utf-8").split("## Keys", 1)[1]
    documented = set(re.findall(r"^\| `([a-z0-9_.]+)` \|", doc, flags=re.MULTILINE))
    expected = set(KNOWN_KEYS)
    expected |= {f"reconcile.{k}" for k in KNOWN_RECONCILE_KEYS}
    expected |= {f"tat.{k}" for k in KNOWN_TAT_KEYS}
    expected |= {f"lint.{k}" for k in KNOWN_LINT_KEYS}
    expected |= {f"fhir.identifier_systems.{k}" for k in IDENTIFIER_KINDS}
    expected -= {"reconcile", "tat", "lint", "fhir"}
    assert documented == expected


# -- Dockerfile -------------------------------------------------------------------------

DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _arg(name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE, flags=re.MULTILINE)
    assert match, name
    return match.group(1)


def test_base_images_are_pinned_by_digest():
    for name in ("PYTHON_IMAGE", "UV_IMAGE"):
        assert DIGEST.search(_arg(name)), name
    for line in DOCKERFILE.splitlines():
        if line.startswith("FROM "):
            image = line.split()[1]
            assert image.startswith("${"), f"FROM must use a pinned ARG: {line}"


def test_image_runs_as_a_non_root_user():
    users = re.findall(r"^USER (\S+)$", DOCKERFILE, flags=re.MULTILINE)
    assert users, "no USER line"
    final = users[-1].split(":")[0]
    assert final not in ("0", "root")


def test_image_entrypoint_command_healthcheck_and_labels():
    assert 'ENTRYPOINT ["hl7bridge"]' in DOCKERFILE
    assert 'CMD ["listen", "--host", "0.0.0.0", "--port", "2575"]' in DOCKERFILE
    assert "HEALTHCHECK" in DOCKERFILE and "create_connection(('127.0.0.1', 2575)" in DOCKERFILE
    for label in ("source", "version", "licenses"):
        assert f"org.opencontainers.image.{label}=" in DOCKERFILE
    assert 'org.opencontainers.image.licenses="Apache-2.0"' in DOCKERFILE


def test_image_version_label_matches_the_package():
    assert _arg("VERSION") == __version__


def test_image_installs_locked_hashed_dependencies():
    assert "uv export --frozen --no-dev" in DOCKERFILE
    assert "--require-hashes" in DOCKERFILE
    assert "pip install --no-deps /tmp/dist/*.whl" in DOCKERFILE


def test_uv_version_matches_ci():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_version = re.search(r'UV_VERSION: "([0-9.]+)"', ci)
    assert ci_version
    assert f"ghcr.io/astral-sh/uv:{ci_version.group(1)}@" in DOCKERFILE


def test_dockerignore_is_an_allowlist():
    lines = [
        line
        for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert lines[0] == "*"
    assert {"!src/", "!pyproject.toml", "!uv.lock"} <= set(lines)


# -- compose files ----------------------------------------------------------------------


def _compose(name: str) -> dict:
    return yaml.safe_load((REPO / name).read_text(encoding="utf-8"))


def test_compose_demo_shape():
    services = _compose("compose.demo.yaml")["services"]
    assert set(services) == {"hapi", "bridge", "demo"}
    assert "ports" not in services["hapi"], "HAPI has no host port by default"
    assert services["bridge"]["ports"] == ["127.0.0.1:2575:2575"]
    demo = services["demo"]
    assert demo["depends_on"]["bridge"]["condition"] == "service_healthy"
    assert demo["command"][:3] == ["demo", "--fhir-base", "http://hapi:8080/fhir"]
    for service in services.values():
        for volume in service.get("volumes", []):
            source = volume.split(":", 1)[0]
            assert not source.startswith((".", "/", "~")), f"host folder mounted: {volume}"
    assert set(_compose("compose.demo.yaml")["volumes"]) == {"bridge-data"}
    # The demo uses a temporary folder in its container, so `up` can run again.
    assert "--data-dir" not in demo["command"]
    assert "volumes" not in demo


def test_compose_images_are_pinned():
    for name in ("compose.demo.yaml",):
        for service in _compose(name)["services"].values():
            image = service.get("image", "")
            if service.get("build") or image == "hl7bridge:local":
                continue
            assert DIGEST.search(image), image


def test_hapi_override_is_loopback_only():
    assert _compose("compose.hapi-port.yaml")["services"]["hapi"]["ports"] == [
        "127.0.0.1:8080:8080"
    ]


def test_compose_demo_command_parses():
    from hl7bridge import cli

    command = _compose("compose.demo.yaml")["services"]["demo"]["command"]
    args = cli.build_parser().parse_args(command)
    assert args.command == "demo" and args.fhir_wait == 300


# -- the published-port check ------------------------------------------------------------


def test_repository_ports_are_all_loopback():
    assert check_ports.check_tree(REPO) == []


@pytest.mark.parametrize(
    ("spec", "host"),
    [
        ("2575", None),
        ("2575:2575", None),
        ("127.0.0.1:2575:2575", "127.0.0.1"),
        ("0.0.0.0:2575:2575", "0.0.0.0"),
        ("127.0.0.1:2575:2575/tcp", "127.0.0.1"),
        ("[::1]:2575:2575", "::1"),
    ],
)
def test_short_port_host(spec, host):
    assert check_ports.host_of_short_port(spec) == host


def test_compose_check_catches_open_ports():
    data = {
        "services": {
            "a": {"ports": ["2575:2575"]},
            "b": {"ports": ["0.0.0.0:80:80"]},
            "c": {"ports": [{"target": 80, "published": 8080}]},
            "d": {"network_mode": "host"},
            "e": {"ports": [{"target": 2575}]},
            "f": {"ports": ["2575"]},
            "g": {"ports": [{"target": 80, "published": 8080, "host_ip": "0.0.0.0"}]},
            "ok": {
                "ports": [
                    "127.0.0.1:1:1",
                    {"published": 9, "host_ip": "127.0.0.1"},
                    {"target": 9, "host_ip": "127.0.0.1"},
                ]
            },
        }
    }
    problems = check_ports.check_compose(data, "x.yaml")
    assert sorted(p.where.split()[-1] for p in problems) == ["a", "b", "c", "d", "e", "f", "g"]


def test_workflow_service_ports_are_checked():
    data = {
        "jobs": {
            "a": {"services": {"db": {"ports": ["8080:8080"]}}},
            "b": {"container": {"image": "x", "ports": ["9000:9000"]}},
            "c": {"services": {"db": {"options": "--network host"}}},
            "ok": {"services": {"db": {"ports": ["127.0.0.1:8080:8080"]}}},
        }
    }
    problems = check_ports.check_workflow(data, "ci.yml")
    assert sorted(p.where.split()[1].split(".")[1] for p in problems) == ["a", "b", "c"]


def test_compose_files_are_found_by_content_not_name(tmp_path):
    for name in ("stack.yml", "demo.compose.yaml", "Compose.yaml", "included.yaml"):
        (tmp_path / name).write_text(
            "services:\n  x:\n    ports: ['9000:9000']\n", encoding="utf-8"
        )
    (tmp_path / "not-compose.yaml").write_text(
        "services: [a, b]\nports: ['1:1']\n", encoding="utf-8"
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  t:\n    services:\n      h:\n        ports: ['8080:8080']\n",
        encoding="utf-8",
    )
    problems = check_ports.check_tree(tmp_path)
    assert sorted({p.where.split()[0] for p in problems}) == [
        ".github/workflows/ci.yml",
        "Compose.yaml",
        "demo.compose.yaml",
        "included.yaml",
        "stack.yml",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "docker run -p 8080:8080 img",
        "docker run --publish 8080:8080 img",
        "docker run --publish=0.0.0.0:8080:8080 img",
        "docker run -p8080:8080 img",
        "docker run -P img",
        "sudo docker container run -d -p 2575:2575 img",
        "docker run -dp 8080:8080 img",
        "docker run -itp 8080:8080 img",
        "docker run -dp8080:8080 img",
        "docker run -dP img",
        "docker run --network host img",
        "docker run --network=host img",
        "docker run --net=host img",
        "docker run --net host img",
        "docker create -p 8080:8080 img",
        "docker container create -p 8080:8080 img",
        "podman run -p 8080:8080 img",
        "docker compose run -p 8080:8080 web",
        "docker-compose run --publish 8080:8080 web",
        "docker run -p 2575 img",
    ],
)
def test_docker_run_check_catches_open_ports(line):
    assert check_ports.check_docker_run_line(line, "x") != []


@pytest.mark.parametrize(
    "line",
    [
        "docker run -p 127.0.0.1:8080:8080 img",
        'docker run -d --name "$X" -p 127.0.0.1:8080:8080 "$IMG"',
        "docker run --rm hl7bridge:local demo",
        "docker build -t x .",
        "docker run -dp 127.0.0.1:8080:8080 img",
        "docker run --network bridge -p 127.0.0.1:1:1 img",
        "docker run -e PORT=8080 -v data:/data img",
        "docker compose run --rm demo",
    ],
)
def test_docker_run_check_allows_loopback(line):
    assert check_ports.check_docker_run_line(line, "x") == []


def test_port_check_reads_continued_lines(tmp_path):
    (tmp_path / "run.sh").write_text("docker run \\\n  -p 9000:9000 img\n", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text(
        "services:\n  x:\n    ports: ['9000:9000']\n", encoding="utf-8"
    )
    problems = check_ports.check_tree(tmp_path)
    assert len(problems) == 2


# -- the license check ----------------------------------------------------------------


def test_runtime_dependencies_are_on_the_allow_list():
    results = check_licenses.walk("hl7bridge")
    names = {r.name.lower().replace("_", "-").replace(".", "-") for r in results}
    assert {"hl7", "httpx", "pydicom", "pyyaml", "fhir-resources"} <= names
    assert [r for r in results if not r.allowed] == []
    assert not any(r.name.lower() in ("pytest", "ruff", "mypy") for r in results), (
        "dev tools are not runtime dependencies"
    )


class _Meta:
    def __init__(self, **fields: object) -> None:
        self.fields = fields

    def get(self, key: str, default: object = None) -> object:
        return self.fields.get(key, default)

    def get_all(self, key: str) -> object:
        return self.fields.get(key + "s")

    def __getitem__(self, key: str) -> object:
        return self.fields[key]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"License-Expression": "MIT"}, "MIT"),
        ({"License": "BSD license"}, "BSD"),
        ({"Classifiers": ["License :: OSI Approved :: MIT License"]}, "MIT"),
        ({"License": "GPL-3.0-only"}, "GPL-3.0-only"),
        ({"License": "UNKNOWN", "Classifiers": ["License :: OSI Approved :: MIT License"]}, "MIT"),
        ({"License": "Some very long license text\nwith lines"}, None),
        ({"Classifiers": ["License :: OSI Approved"]}, None),
        ({}, None),
        (
            {
                "License": "GPL-3.0-or-later",
                "Classifiers": ["License :: OSI Approved :: BSD License"],
            },
            "BSD AND GPL-3.0-or-later",
        ),
        (
            {
                "Classifiers": [
                    "License :: OSI Approved :: MIT License",
                    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
                ]
            },
            "(GNU General Public License v3 (GPLv3)) AND MIT",
        ),
    ],
)
def test_license_of(fields, expected):
    assert check_licenses.license_of(_Meta(**fields)) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("license", "allowed"),
    [
        ("MIT", True),
        ("MIT OR Apache-2.0", True),
        ("GPL-3.0-only", False),
        ("MIT AND GPL-2.0-or-later", False),
        ("BSD AND GPL-3.0-or-later", False),
        ("(GNU General Public License v3 (GPLv3)) AND MIT", False),
        (None, False),
    ],
)
def test_allow_list(license, allowed):
    assert check_licenses.Result("x", "1", license).allowed is allowed


def test_license_check_fails_on_a_copyleft_dependency(monkeypatch):
    class Dist:
        def __init__(self, name, requires, lic):
            self.metadata = _Meta(Name=name, **{"License-Expression": lic})
            self.version = "1"
            self.requires = requires

    tree = {
        "root": Dist("root", ["good", "bad; python_version < '2'", "worse"], "MIT"),
        "good": Dist("good", [], "MIT"),
        "worse": Dist("worse", [], "GPL-3.0-only"),
    }
    results = check_licenses.walk("root", get=lambda n: tree[n])  # type: ignore[arg-type,return-value]
    assert [(r.name, r.allowed) for r in results] == [("good", True), ("worse", False)]


def test_hl7bridge_metadata_is_apache():
    assert metadata.metadata("hl7bridge")["License-Expression"] == "Apache-2.0"


def test_every_ci_job_has_a_timeout():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    jobs = workflow["jobs"]
    assert jobs
    for name, job in jobs.items():
        assert isinstance(job.get("timeout-minutes"), int), name
        assert job["timeout-minutes"] <= 30, name


def test_image_build_dependencies_are_hashed():
    """The build stage installs hatchling from the hashed build lock, so the
    wheel is built with pinned tools, not whatever PyPI serves that day."""
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "--build-constraints .github/requirements/build.txt" in dockerfile
    assert "--require-hashes" in dockerfile.split("AS runtime")[0]
    assert "!.github/requirements/build.txt" in (REPO / ".dockerignore").read_text("utf-8")
    lock = (REPO / ".github" / "requirements" / "build.txt").read_text(encoding="utf-8")
    assert re.search(r"^hatchling==\S+ \\$", lock, re.MULTILINE)
    assert "--hash=sha256:" in lock
