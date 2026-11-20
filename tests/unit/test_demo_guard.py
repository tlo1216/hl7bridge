"""Deletion boundary of scripts/demo.sh: it deletes only a folder it created.

Runs the sourced guard function in bash. Skips where no POSIX bash is
available (a Windows machine without Git Bash, or one whose bash is WSL,
which cannot see Windows paths the same way), unless
HL7BRIDGE_REQUIRE_BASH=1, which CI sets so the skip can never hide a
missing run.
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
REQUIRE_BASH = os.environ.get("HL7BRIDGE_REQUIRE_BASH") == "1"
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")


def _is_posix_bash(bash: str) -> bool:
    try:
        kind = subprocess.run(
            [bash, "-c", "uname -s"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return kind.startswith(("MINGW", "MSYS", "CYGWIN"))


def _bash() -> str | None:
    if sys.platform != "win32":
        return shutil.which("bash")
    # Git Bash first: the bash on PATH can be the WSL stub in System32.
    for candidate in (str(GIT_BASH), shutil.which("bash")):
        if candidate and Path(candidate).exists() and _is_posix_bash(candidate):
            return candidate
    return None


BASH = _bash()
pytestmark = pytest.mark.skipif(
    BASH is None and not REQUIRE_BASH, reason="needs a POSIX bash (Git Bash on Windows)"
)


def run_guard(target: Path | str, *, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None, "HL7BRIDGE_REQUIRE_BASH=1 but no POSIX bash was found"
    env = {**os.environ, "HOME": home.as_posix()}
    script = 'source "$1"; prepare_data_dir "$2" "$3"'
    target_text = target.as_posix() if isinstance(target, Path) else target
    return subprocess.run(
        [BASH, "-c", script, "guard", GUARD.as_posix(), target_text, REPO_ROOT.as_posix()],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _make_dir_link(link: Path, target: Path) -> str:
    """A directory symlink, or on Windows without the symlink right, a
    junction (which Git Bash also reports as a symlink). Returns the kind."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink"
    except OSError:
        if sys.platform != "win32":
            raise
    import _winapi  # Windows only; creating a junction needs no extra right

    _winapi.CreateJunction(str(target), str(link))
    return "junction"


def _case_variant(path: Path) -> Path | None:
    """The same folder spelled in upper case, if the file system folds case."""
    variant = Path(str(path).upper())
    if str(variant) == str(path) or not variant.exists():
        return None
    return variant


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


def test_owned_folder_with_trailing_slash_is_reset(tmp_path, home):
    target = tmp_path / "demo"
    assert run_guard(target, cwd=tmp_path, home=home).returncode == 0
    (target / "old.db").write_text("stale", encoding="utf-8")
    result = run_guard(target.as_posix() + "//", cwd=tmp_path, home=home)
    assert result.returncode == 0, result.stderr
    assert not (target / "old.db").exists()
    assert (target / MARKER).is_file()


def test_path_with_spaces_is_created_and_reset(tmp_path, home):
    target = tmp_path / "my demo data" / "run one"
    result = run_guard(target, cwd=tmp_path, home=home)
    assert result.returncode == 0, result.stderr
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
    assert not (target / MARKER).exists()


@pytest.mark.parametrize("planted", [False, True])
@pytest.mark.parametrize("form", ["missing/../existing", "existing/../existing", "./a/../existing"])
def test_dotdot_component_is_refused(tmp_path, home, form, planted):
    """``missing/../existing``: -e is false (missing does not exist) but
    mkdir -p would reach the existing folder and mark it."""
    existing = tmp_path / "existing"
    existing.mkdir()
    keep = existing / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    if planted:
        (existing / MARKER).write_text("planted", encoding="utf-8")
    result = run_guard(form, cwd=tmp_path, home=home)
    assert result.returncode != 0
    assert ".. component" in result.stderr
    assert keep.read_text(encoding="utf-8") == "keep"
    assert (existing / MARKER).exists() is planted
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("which", ["home", "cwd", "parent_of_cwd", "parent_of_home", "repo_root"])
def test_protected_folders_are_refused_even_with_a_marker(tmp_path, home, which):
    cwd = tmp_path / "work" / "inner"
    cwd.mkdir(parents=True)
    if which == "repo_root":
        # Never plant a marker in the real repo; the root check runs first.
        target = REPO_ROOT
    else:
        target = {
            "home": home,
            "cwd": cwd,
            "parent_of_cwd": cwd.parent,
            "parent_of_home": home.parent,
        }[which]
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


@pytest.mark.parametrize("which", ["home", "cwd", "parent_of_cwd"])
def test_protected_folders_are_refused_in_another_spelling(tmp_path, home, which):
    """On a case-folding file system (Windows, default macOS) the same
    folder spelled in upper case must still be refused."""
    cwd = tmp_path / "work" / "inner"
    cwd.mkdir(parents=True)
    target = {"home": home, "cwd": cwd, "parent_of_cwd": cwd.parent}[which]
    variant = _case_variant(target)
    if variant is None:
        pytest.skip("file system is case-sensitive; no second spelling exists")
    (target / MARKER).write_text("planted", encoding="utf-8")
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    result = run_guard(variant, cwd=cwd, home=home)
    assert result.returncode != 0
    assert "refusing" in result.stderr
    assert "marker" not in result.stderr
    assert sentinel.exists()


def test_a_file_is_refused(tmp_path, home):
    target = tmp_path / "afile"
    target.write_text("x", encoding="utf-8")
    result = run_guard(target, cwd=tmp_path, home=home)
    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "x"


@pytest.mark.parametrize("suffix", ["", "/", "//"])
def test_link_is_refused_with_or_without_trailing_slash(tmp_path, home, suffix):
    real = tmp_path / "real"
    real.mkdir()
    (real / MARKER).write_text("planted", encoding="utf-8")
    (real / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    _make_dir_link(link, real)
    result = run_guard(link.as_posix() + suffix, cwd=tmp_path, home=home)
    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert (real / "keep.txt").exists()
    assert (real / MARKER).exists()
