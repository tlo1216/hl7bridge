"""Deletion boundary of scripts/demo.sh: it deletes only a folder it created.

Runs the sourced guard function in bash. Skips where no POSIX bash is
available (a Windows machine without Git Bash, or one whose bash is WSL,
which cannot see Windows paths the same way), unless
HL7BRIDGE_REQUIRE_BASH=1, which CI sets so the skip can never hide a
missing run.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "scripts" / "demo_guard.sh"
REPO_ROOT = GUARD.parents[1]
MARKER = ".hl7bridge-demo"
SHELL_MARKER_TEXT = "Created by scripts/demo.sh. Holds synthetic data only. Safe to delete."
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
        (existing / MARKER).write_text(SHELL_MARKER_TEXT + "\n", encoding="utf-8")
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
        (target / MARKER).write_text(SHELL_MARKER_TEXT + "\n", encoding="utf-8")
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
    (target / MARKER).write_text(SHELL_MARKER_TEXT + "\n", encoding="utf-8")
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
    (real / MARKER).write_text(SHELL_MARKER_TEXT + "\n", encoding="utf-8")
    (real / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    _make_dir_link(link, real)
    result = run_guard(link.as_posix() + suffix, cwd=tmp_path, home=home)
    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert (real / "keep.txt").exists()
    assert (real / MARKER).exists()


def test_marker_written_by_the_python_demo_is_refused(tmp_path, home):
    """`hl7bridge demo` deletes only the files its JSON marker lists. The
    shell guard deletes whole folders, so it must not accept that marker."""
    target = tmp_path / "pydemo"
    target.mkdir()
    (target / MARKER).write_text(json.dumps({"tool": "hl7bridge", "files": []}), encoding="utf-8")
    keep = target / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    result = run_guard(target, cwd=tmp_path, home=home)
    assert result.returncode != 0
    assert "not written by scripts/demo.sh" in result.stderr
    assert keep.exists()


def test_the_shell_marker_is_written_and_accepted(tmp_path, home):
    target = tmp_path / "demo"
    assert run_guard(target, cwd=tmp_path, home=home).returncode == 0
    first = (target / MARKER).read_text(encoding="utf-8").splitlines()[0]
    assert first == SHELL_MARKER_TEXT


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _console_script() -> str:
    """The installed ``hl7bridge`` entry point. On Windows it is a small
    launcher .exe that starts Python as a child process: the shape of the bug."""
    scripts = Path(sys.executable).parent
    found = shutil.which("hl7bridge", path=str(scripts)) or shutil.which("hl7bridge")
    assert found, "the hl7bridge console script is not installed"
    return Path(found).as_posix()


def test_stop_pid_ends_the_whole_process_tree(tmp_path):
    """Reproduces the Windows case: $! is a launcher whose Python child holds
    the port. A plain kill leaves the child listening; demo_stop_pid must
    not. On Linux and macOS the launcher is the listener itself."""
    assert BASH is not None, "HL7BRIDGE_REQUIRE_BASH=1 but no POSIX bash was found"
    port = _free_port()
    db = (tmp_path / "stop.db").as_posix()
    script = (
        # Backgrounding through eval, as scripts often do: then $! is a bash
        # subshell, the launcher is its child and Python the grandchild.
        'source "$1"; eval "$2" >"$5" 2>&1 & pid=$!; '
        "for _ in $(seq 1 150); do "
        '(exec 3<>"/dev/tcp/127.0.0.1/$3") 2>/dev/null && break; sleep 0.1; done; '
        'demo_stop_pid "$pid"; echo stopped; '
        # Check from inside the same session: once bash exits, Windows may
        # end the orphan anyway, which would hide the bug.
        "for _ in $(seq 1 20); do "
        '(exec 3<>"/dev/tcp/127.0.0.1/$3") 2>/dev/null || { echo closed; exit 0; }; '
        "sleep 0.25; done; "
        "echo still-listening"
    )
    result = subprocess.run(
        [
            BASH,
            "-c",
            script,
            "stop",
            GUARD.as_posix(),
            shlex.join([_console_script(), "-q", "listen", "--port", str(port), "--db", db]),
            str(port),
            db,
            (tmp_path / "listen.log").as_posix(),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert "stopped" in result.stdout
    assert "closed" in result.stdout, "the listener outlived demo_stop_pid"
    deadline = time.monotonic() + 10
    while _port_open(port) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _port_open(port), "the listener outlived demo_stop_pid"
