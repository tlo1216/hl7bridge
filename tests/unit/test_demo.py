"""``hl7bridge demo`` and its deletion boundary.

``remove_demo_dir`` is the only code in hl7bridge that deletes. These tests
plant every case the plan lists: no marker, a symlink or junction, the home
folder, the current folder and its parent, ``..`` segments, a foreign file
next to demo files, a folder swapped for a link between the check and the
delete, another device, and markers written by something else.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from hl7bridge import cli
from hl7bridge import demo as demo_module
from hl7bridge.demo import (
    MARKER,
    DemoOptions,
    claim_demo_dir,
    read_marker,
    remove_demo_dir,
    run_demo,
)
from hl7bridge.errors import EXIT_CONNECTION, EXIT_OK, EXIT_USAGE, DemoDirError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.profiles import BUNDLED_PROFILES


def _make_dir_link(link: Path, target: Path) -> str:
    """A directory symlink, or on Windows without the symlink right, a junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink"
    except OSError:
        if sys.platform != "win32":
            raise
    import _winapi  # Windows only; a junction needs no extra right

    _winapi.CreateJunction(str(target), str(link))
    return "junction"


def _unlink_dir_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)  # removes a junction, never its target


@pytest.fixture
def owned(tmp_path) -> Path:
    """A demo folder with two demo files in a subfolder, as a run leaves it."""
    target = tmp_path / "run"
    claimed = claim_demo_dir(target)
    sub = claimed.mkdir("synth")
    (sub / "a.hl7").write_bytes(b"MSH|demo\r")
    (sub / "b.hl7").write_bytes(b"MSH|demo\r")
    claimed.record_tree("synth")
    return target


@pytest.fixture
def fake_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home
    return home


# -- claiming a folder ---------------------------------------------------------------


def test_default_is_a_fresh_temporary_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    claimed = claim_demo_dir(None)
    assert claimed.path.parent == tmp_path
    assert claimed.path.name.startswith("hl7bridge-demo-")
    marker = read_marker(claimed.path)
    assert marker["tool"] == "hl7bridge" and marker["created_dir"] is True


def test_new_path_is_created_and_marked(tmp_path):
    claimed = claim_demo_dir(tmp_path / "a" / "b")
    assert claimed.created_dir
    assert read_marker(tmp_path / "a" / "b")["device"] == os.lstat(tmp_path / "a" / "b").st_dev


def test_empty_existing_folder_is_used_but_never_removed(tmp_path):
    target = tmp_path / "empty"
    target.mkdir()
    claimed = claim_demo_dir(target)
    assert claimed.created_dir is False
    result = remove_demo_dir(target)
    assert not result.root_removed
    assert target.is_dir() and not any(target.iterdir())


def test_non_empty_folder_without_marker_is_refused(tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(DemoDirError, match=r"has no .hl7bridge-demo marker"):
        claim_demo_dir(target)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (target / MARKER).exists()


def test_earlier_demo_folder_is_reused(owned):
    claimed = claim_demo_dir(owned)
    assert claimed.created_dir is True, "ownership of the folder carries over"
    assert not (owned / "synth").exists(), "the old demo files were removed first"
    assert read_marker(owned)["files"] == []


def test_earlier_demo_folder_with_a_foreign_file_is_refused(owned):
    (owned / "mine.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(DemoDirError, match="did not write are still in it"):
        claim_demo_dir(owned)
    assert (owned / "mine.txt").exists()


def test_a_file_is_refused(tmp_path):
    target = tmp_path / "afile"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(DemoDirError, match="not a folder"):
        claim_demo_dir(target)


def test_claim_refuses_dotdot(tmp_path):
    (tmp_path / "existing").mkdir()
    with pytest.raises(DemoDirError, match=r"'\.\.' component"):
        claim_demo_dir(tmp_path / "missing" / ".." / "existing")
    assert not (tmp_path / "existing" / MARKER).exists()
    assert not (tmp_path / "missing").exists()


def test_claim_refuses_a_link(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _make_dir_link(link, real)
    with pytest.raises(DemoDirError, match="symlink or junction"):
        claim_demo_dir(link)
    assert not (real / MARKER).exists()


def test_claim_refuses_the_current_folder(tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    with pytest.raises(DemoDirError, match="current folder"):
        claim_demo_dir(cwd)
    assert not (cwd / MARKER).exists()


# -- remove_demo_dir -------------------------------------------------------------------


def test_owned_folder_is_removed_completely(owned):
    result = remove_demo_dir(owned)
    assert result.root_removed
    assert result.removed_files == 2 and result.removed_dirs == 1
    assert not owned.exists()


def test_folder_without_marker_is_never_deleted(tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(DemoDirError, match=r"has no .hl7bridge-demo marker"):
        remove_demo_dir(target)
    assert (target / "keep.txt").exists()


@pytest.mark.parametrize("suffix", ["", "/", "//"])
def test_link_to_an_owned_folder_is_refused(owned, tmp_path, suffix):
    link = tmp_path / "link"
    _make_dir_link(link, owned)
    try:
        with pytest.raises(DemoDirError, match="symlink or junction"):
            remove_demo_dir(Path(str(link) + suffix))
        assert (owned / "synth" / "a.hl7").exists()
        assert (owned / MARKER).exists()
    finally:
        _unlink_dir_link(link)


def test_home_folder_is_refused(fake_home):
    (fake_home / MARKER).write_text(
        json.dumps({"tool": "hl7bridge", "files": [], "dirs": [], "device": 0}), encoding="utf-8"
    )
    (fake_home / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(DemoDirError, match="your home folder"):
        remove_demo_dir(fake_home)
    assert (fake_home / "keep.txt").exists() and (fake_home / MARKER).exists()


def test_parent_of_home_is_refused(fake_home):
    parent = fake_home.parent
    with pytest.raises(DemoDirError, match="your home folder"):
        remove_demo_dir(parent)


def test_current_folder_and_its_parent_are_refused(owned, monkeypatch):
    inner = owned / "synth"
    monkeypatch.chdir(inner)
    with pytest.raises(DemoDirError, match="current folder"):
        remove_demo_dir(owned)  # a parent of the current folder
    monkeypatch.chdir(owned)
    with pytest.raises(DemoDirError, match="current folder"):
        remove_demo_dir(owned)  # the current folder itself
    with pytest.raises(DemoDirError, match="current folder"):
        remove_demo_dir(Path("."))
    assert (inner / "a.hl7").exists()


def test_protected_folder_in_another_spelling_is_refused(fake_home):
    variant = Path(str(fake_home).upper())
    if str(variant) == str(fake_home) or not variant.exists():
        pytest.skip("this file system does not fold case")
    with pytest.raises(DemoDirError, match="your home folder"):
        remove_demo_dir(variant)


def test_repository_root_is_refused():
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(DemoDirError, match="repository root"):
        remove_demo_dir(root)


def test_drive_root_is_refused():
    root = Path(Path.cwd().anchor)
    with pytest.raises(DemoDirError, match="root"):
        remove_demo_dir(root)


def test_dotdot_is_refused(owned):
    with pytest.raises(DemoDirError, match=r"'\.\.' component"):
        remove_demo_dir(owned / "synth" / "..")
    assert (owned / "synth" / "a.hl7").exists()


def test_foreign_file_is_left_and_reported(owned):
    foreign = owned / "synth" / "mine.txt"
    foreign.write_text("keep", encoding="utf-8")
    result = remove_demo_dir(owned)
    assert foreign.read_text(encoding="utf-8") == "keep"
    assert not (owned / "synth" / "a.hl7").exists(), "demo files are removed"
    assert result.left == ["synth"], "the folder holding the foreign file stays"
    assert not result.root_removed and owned.is_dir()
    assert not (owned / MARKER).exists()


def test_folder_swapped_for_a_link_between_check_and_delete(owned, tmp_path, monkeypatch):
    """The checks pass, then synth/ is replaced by a link to an outside
    folder holding files with the same names. Nothing outside is deleted."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.hl7").write_text("outside a", encoding="utf-8")
    (outside / "b.hl7").write_text("outside b", encoding="utf-8")

    def swap() -> None:
        shutil.move(str(owned / "synth"), str(tmp_path / "moved-synth"))
        _make_dir_link(owned / "synth", outside)

    monkeypatch.setattr(demo_module, "_between_check_and_delete", swap)
    try:
        result = remove_demo_dir(owned)
        assert (outside / "a.hl7").read_text(encoding="utf-8") == "outside a"
        assert (outside / "b.hl7").read_text(encoding="utf-8") == "outside b"
        assert set(result.refused) >= {"synth/a.hl7", "synth/b.hl7"}
        assert not result.root_removed
    finally:
        _unlink_dir_link(owned / "synth")
    assert (outside / "a.hl7").exists()


def test_root_swapped_for_a_link_stops_everything(owned, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"

    def swap() -> None:
        shutil.move(str(owned), str(elsewhere))
        _make_dir_link(owned, elsewhere)

    monkeypatch.setattr(demo_module, "_between_check_and_delete", swap)
    try:
        with pytest.raises(DemoDirError, match="changed while it was being removed"):
            remove_demo_dir(owned)
        assert (elsewhere / "synth" / "a.hl7").exists()
    finally:
        _unlink_dir_link(owned)


def test_other_device_is_refused(owned):
    marker = owned / MARKER
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["device"] += 1
    marker.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DemoDirError, match="not on the device"):
        remove_demo_dir(owned)
    assert (owned / "synth" / "a.hl7").exists()


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("Created by scripts/demo.sh. Holds synthetic data only. Safe to delete.\n", "demo.sh"),
        (json.dumps({"tool": "othertool", "files": [], "dirs": [], "device": 1}), "another tool"),
        (json.dumps({"tool": "hl7bridge", "files": "all", "dirs": [], "device": 1}), "damaged"),
        (json.dumps({"tool": "hl7bridge", "files": [], "dirs": []}), "damaged"),
    ],
    ids=["shell_marker", "other_tool", "bad_files", "no_device"],
)
def test_foreign_markers_are_refused(tmp_path, content, why):
    target = tmp_path / "x"
    target.mkdir()
    (target / MARKER).write_text(content, encoding="utf-8")
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(DemoDirError, match=why):
        remove_demo_dir(target)
    assert (target / "keep.txt").exists()


def test_marker_that_is_a_link_is_refused(tmp_path):
    target = tmp_path / "x"
    target.mkdir()
    real_marker_dir = tmp_path / "m"
    real_marker_dir.mkdir()
    _make_dir_link(target / MARKER, real_marker_dir)
    try:
        with pytest.raises(DemoDirError, match="not a regular file"):
            remove_demo_dir(target)
    finally:
        _unlink_dir_link(target / MARKER)


@pytest.mark.parametrize(
    "entry", ["../escape.txt", "/abs.txt", "a/../../escape.txt", "C:/x.txt", "a\\..\\x", ""]
)
def test_marker_entries_cannot_escape(owned, tmp_path, entry):
    escape = tmp_path / "escape.txt"
    escape.write_text("keep", encoding="utf-8")
    marker = owned / MARKER
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["files"].append(entry)
    marker.write_text(json.dumps(data), encoding="utf-8")
    result = remove_demo_dir(owned)
    assert escape.read_text(encoding="utf-8") == "keep"
    assert entry in result.refused


def test_listed_file_replaced_by_a_link_is_not_followed(owned, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inner.txt").write_text("keep", encoding="utf-8")
    (owned / "synth" / "a.hl7").unlink()
    _make_dir_link(owned / "synth" / "a.hl7", outside)
    try:
        result = remove_demo_dir(owned)
        assert "synth/a.hl7" in result.refused
        assert (outside / "inner.txt").exists()
    finally:
        _unlink_dir_link(owned / "synth" / "a.hl7")


def test_remove_is_the_only_deleting_code():
    """Every delete call in the package lives in remove_demo_dir's helpers."""
    src = Path(demo_module.__file__).resolve().parent
    pattern = ("rmtree", ".unlink(", "os.remove(", "os.unlink(", ".rmdir(", "os.rmdir(")
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(p in text for p in pattern) and path.name != "demo.py":
            offenders.append(path.name)
    assert offenders == []


# -- the demo run -----------------------------------------------------------------------


@pytest.fixture
def temp_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    monkeypatch.chdir(tmp_path)
    return root


def test_demo_runs_checks_the_manifest_and_cleans_up(temp_root, capsys):
    rc = cli.main(["demo"])
    out = capsys.readouterr().out
    assert rc == EXIT_OK, out
    assert "every ACK and quarantine count matches the manifest" in out
    assert "listener on 127.0.0.1" in out
    assert "skipped: no server named" in out
    assert "removed the demo data folder" in out
    assert list(temp_root.iterdir()) == [], "the temporary folder is gone"


def test_demo_keep_leaves_the_folder(temp_root, capsys):
    assert cli.main(["demo", "--keep"]) == EXIT_OK
    kept = list(temp_root.iterdir())
    assert len(kept) == 1
    assert (kept[0] / "bridge.db").is_file()
    assert (kept[0] / "synth" / "manifest.json").is_file()
    assert "demo data kept in" in capsys.readouterr().out
    files = set(read_marker(kept[0])["files"])
    assert {"bridge.db", "synth/manifest.json"} <= files
    assert remove_demo_dir(kept[0]).root_removed


def test_demo_data_dir_twice(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "demo-data"
    assert cli.main(["demo", "--data-dir", str(target)]) == EXIT_OK
    assert cli.main(["demo", "--data-dir", str(target)]) == EXIT_OK, "an earlier demo folder"
    assert (target / "bridge.db").exists(), "a named folder is kept"


def test_demo_refuses_a_folder_it_does_not_own(tmp_path, capsys):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    assert cli.main(["demo", "--data-dir", str(target)]) == EXIT_USAGE
    assert "error: refusing to use" in capsys.readouterr().err
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]


def _fhir_ok():
    counter = iter(range(1, 1_000_000))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"resourceType": "CapabilityStatement"})
        return httpx.Response(201, json={"id": str(next(counter))})

    return handler


def _factory(handler):
    def make(base_url: str, **kwargs) -> HapiClient:
        return HapiClient(base_url, transport=httpx.MockTransport(handler), **kwargs)

    return make


def test_demo_syncs_to_a_named_fhir_server(tmp_path, capsys):
    opts = DemoOptions(
        profiles=BUNDLED_PROFILES,
        data_dir=tmp_path / "d",
        fhir_base="http://fhir.test/fhir",
        client_factory=_factory(_fhir_ok()),
    )
    rc = run_demo(opts, sys.stdout, sys.stderr)
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "syncing to http://fhir.test/fhir" in out
    assert "Patient: created" in out


def test_demo_fhir_server_down_is_a_connection_error_and_keeps_data(tmp_path, capsys):
    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    opts = DemoOptions(
        profiles=BUNDLED_PROFILES,
        fhir_base="http://fhir.test/fhir",
        client_factory=_factory(refused),
    )
    with pytest.raises(Exception) as excinfo:
        run_demo(opts, sys.stdout, sys.stderr)
    assert getattr(excinfo.value, "exit_code", None) == EXIT_CONNECTION
    assert (
        str(excinfo.value)
        == "no answer from http://fhir.test/fhir/metadata. Is the server running?"
    )
    out = capsys.readouterr().out
    kept = out.rsplit("demo data kept in ", 1)[1].strip()
    assert Path(kept).is_dir()
    remove_demo_dir(Path(kept))


def test_demo_waits_for_fhir(tmp_path, capsys, monkeypatch):
    calls = {"n": 0}

    def booting(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503)
        return _fhir_ok()(request)

    monkeypatch.setattr(demo_module.time, "sleep", lambda s: None)
    opts = DemoOptions(
        profiles=BUNDLED_PROFILES,
        data_dir=tmp_path / "d",
        fhir_base="http://fhir.test/fhir",
        fhir_wait_s=600,
        client_factory=_factory(booting),
    )
    assert run_demo(opts, sys.stdout, sys.stderr) == EXIT_OK
    assert calls["n"] == 3
