"""Deletion boundary of scripts/demo.sh: it deletes only a folder it created.

Runs the sourced guard function in bash. Skips where no POSIX bash is
available (a Windows machine without Git Bash, or one whose bash is WSL,
which cannot see Windows paths the same way).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "scripts" / "demo_guard.sh"
REPO_ROOT = GUARD.parents[1]
MARKER = ".hl7bridge-demo"


def _bash() -> str | None:
    bash = shutil.which("bash")
    if bash is None:
        return None
    if sys.platform == "win32":
        try:
            kind = subprocess.run(
                [bash, "-c", "uname -s"], capture_output=True, text=True, timeout=20
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if not kind.startswith(("MINGW", "MSYS", "CYGWIN")):
            return None
    return bash


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="needs a POSIX bash (Git Bash on Windows)")


def run_guard(target: Path, *, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    env = {**os.environ, "HOME": home.as_posix()}
    script = 'source "$1"; prepare_data_dir "$2" "$3"'
    return subprocess.run(
        [BASH, "-c", script, "guard", GUARD.as_posix(), target.as_posix(), REPO_ROOT.as_posix()],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_new_folder_is_created_with_marker(tmp_path, home):
    target = tmp_path / "demo"
    result = run_guard(target, cwd=tmp_path, home=home)
    assert result.returncode == 0, result.stderr
    assert (target / MARKER).is_file()


def test_owned_folder_is_reset(tmp_path, home):
    target = tmp_path / "demo"
    assert run_guard(target, cwd=tmp_path, home=home).returncode == 0
    (target / "old.db").write_text("stale", encoding="utf-8")
    result = run_guard(target, cwd=tmp_path, home=home)
    assert result.returncode == 0, result.stderr
    assert not (target / "old.db").exists()
    assert (target / MARKER).is_file()


def test_folder_without_marker_is_never_deleted(tmp_path, home):
    target = tmp_path / "precious"
    target.mkdir()
    keep = target / "keep.txt"
    keep.write_text("not demo data", encoding="utf-8")
    result = run_guard(target, cwd=tmp_path, home=home)
    assert result.returncode != 0
    assert "no .hl7bridge-demo marker" in result.stderr
    assert keep.read_text(encoding="utf-8") == "not demo data"


@pytest.mark.parametrize("which", ["home", "cwd", "parent_of_cwd", "repo_root"])
def test_protected_folders_are_refused_even_with_a_marker(tmp_path, home, which):
    cwd = tmp_path / "work" / "inner"
    cwd.mkdir(parents=True)
    if which == "repo_root":
        # Never plant a marker in the real repo; the root check runs first.
        target = REPO_ROOT
    else:
        target = {"home": home, "cwd": cwd, "parent_of_cwd": cwd.parent}[which]
        (target / MARKER).write_text("planted", encoding="utf-8")
    sentinel = target / f"sentinel-{which}.txt"
    if which != "repo_root":
        sentinel.write_text("keep", encoding="utf-8")
    result = run_guard(target, cwd=cwd, home=home)
    assert result.returncode != 0
    assert "refusing" in result.stderr
    # Refused by the boundary check itself, not merely by a missing marker.
    assert "marker" not in result.stderr
    assert target.is_dir()
    if which != "repo_root":
        assert sentinel.exists()


def test_a_file_is_refused(tmp_path, home):
    target = tmp_path / "afile"
    target.write_text("x", encoding="utf-8")
    result = run_guard(target, cwd=tmp_path, home=home)
    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "x"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_symlink_is_refused(tmp_path, home):
    real = tmp_path / "real"
    real.mkdir()
    (real / MARKER).write_text("planted", encoding="utf-8")
    (real / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    result = run_guard(link, cwd=tmp_path, home=home)
    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert (real / "keep.txt").exists()
