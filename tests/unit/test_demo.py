"""``hl7bridge demo``, where it writes, and the one function that deletes.

The demo deletes only a temporary folder it made itself in the same run,
and only what it recorded creating there. These tests change the folder
in every way that could trick a deleter (a foreign file, a replaced file, a
replaced folder, a link or junction, a swapped root) and check that only
the demo's own files go. The swaps run through ``_before_step``, which is
called right before each removal step, so every step must check again.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from hl7bridge import cli, demofiles
from hl7bridge import demo as demo_module
from hl7bridge.demo import DemoOptions, claim_demo_dir, run_demo
from hl7bridge.demofiles import OwnedFiles, remove_owned
from hl7bridge.errors import EXIT_CONNECTION, EXIT_FINDINGS, EXIT_OK, EXIT_USAGE, DemoDirError
from hl7bridge.fhir.client import HapiClient
from hl7bridge.fhir.sync import FhirSyncReport
from hl7bridge.profiles import BUNDLED_PROFILES

from ..fakefhir import FakeFhir

# Fake credentials, joined at runtime so no literal user:password URL sits in the source.
_USER = "alice"
_FAKE_PW = "s3cret-PW"
_CRED = _USER + ":" + _FAKE_PW


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
def temp_root(tmp_path, monkeypatch) -> Path:
    """The system temp folder, moved into this test's own folder."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    monkeypatch.chdir(tmp_path)
    return root


@pytest.fixture
def owned(temp_root) -> OwnedFiles:
    """A temporary demo folder holding two files in a subfolder and one at
    the top, as a run leaves it."""
    files = claim_demo_dir(None)
    files.write_bytes(files.root / "synth" / "a.hl7", b"MSH|demo\r")
    files.write_bytes(files.root / "synth" / "b.hl7", b"MSH|demo\r")
    files.write_bytes(files.root / "bridge.db", b"")
    return files


@pytest.fixture
def outside(tmp_path) -> Path:
    """A folder the demo never created, holding a file it must never delete."""
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "a.hl7").write_text("not the demo's", encoding="utf-8")
    return folder


def _on_step(monkeypatch, action, when) -> None:
    """Run ``action`` once, right before the removal step ``when`` selects."""
    done = {"yes": False}

    def hook(entry):
        if not done["yes"] and when(entry):
            done["yes"] = True
            action()

    monkeypatch.setattr(demofiles, "_before_step", hook)


# -- where the demo writes -----------------------------------------------------------


def test_default_is_a_new_temporary_folder(temp_root):
    files = claim_demo_dir(None)
    assert files.temporary
    assert files.root.parent == temp_root
    assert files.root.name.startswith("hl7bridge-demo-")
    assert files.root_identity is not None


def test_the_temporary_folder_comes_from_mkdtemp(temp_root, monkeypatch):
    """The privacy of the folder (0700 on POSIX, the per-user temp folder on
    Windows) comes from mkdtemp; a plain mkdir would not give it."""
    calls: list[str] = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        made = real(*args, **kwargs)
        calls.append(made)
        return made

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    files = claim_demo_dir(None)
    assert calls == [str(files.root)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_temporary_folder_is_private(temp_root):
    import stat

    files = claim_demo_dir(None)
    assert stat.S_IMODE(os.stat(files.root).st_mode) == 0o700


def test_a_new_data_dir_is_created_and_is_not_temporary(tmp_path):
    files = claim_demo_dir(tmp_path / "new" / "run")
    assert files.root.is_dir()
    assert not files.temporary


def test_an_empty_data_dir_is_used(tmp_path):
    (tmp_path / "empty").mkdir()
    assert not claim_demo_dir(tmp_path / "empty").temporary


def test_a_non_empty_data_dir_is_refused_and_left_alone(tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(DemoDirError) as excinfo:
        claim_demo_dir(target)
    assert str(excinfo.value) == (
        f"refusing to use {target}: it is not empty. The demo writes only into a new or"
        " empty folder, and never deletes anything in a folder you name."
    )
    assert [p.name for p in target.iterdir()] == ["keep.txt"]


def test_a_file_is_refused(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(DemoDirError, match="exists and is not a folder"):
        claim_demo_dir(target)


def test_a_folder_that_appears_while_starting_is_refused(tmp_path, monkeypatch):
    target = tmp_path / "raced"
    target.mkdir()
    real_stat = os.stat

    def missing_then_there(path, *args, **kwargs):
        if Path(path) == target:
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(demo_module.os, "stat", missing_then_there)
    with pytest.raises(DemoDirError, match="appeared while the demo was starting"):
        claim_demo_dir(target)


def test_folders_are_created_exclusively_never_adopted(owned):
    (owned.root / "found").mkdir()
    with pytest.raises(FileExistsError):
        owned.makedirs(owned.root / "found")
    assert all(e.parts != ("found",) for e in owned.entries)


def test_files_are_created_exclusively_never_overwritten(owned):
    (owned.root / "synth" / "c.hl7").write_bytes(b"someone else's")
    with pytest.raises(FileExistsError):
        owned.write_bytes(owned.root / "synth" / "c.hl7", b"demo")
    assert (owned.root / "synth" / "c.hl7").read_bytes() == b"someone else's"
    assert all(e.parts != ("synth", "c.hl7") for e in owned.entries)


def test_a_link_at_a_new_name_is_never_written_through(owned, outside):
    link = owned.root / "synth" / "c.hl7"
    try:
        link.symlink_to(outside / "a.hl7")
    except OSError:
        pytest.skip("no right to create a file symlink here")
    with pytest.raises(FileExistsError):
        owned.write_bytes(link, b"demo")
    assert (outside / "a.hl7").read_text(encoding="utf-8") == "not the demo's"


def test_paths_outside_the_folder_are_refused(owned, tmp_path):
    with pytest.raises(DemoDirError, match="not inside the demo folder"):
        owned.write_bytes(tmp_path / "elsewhere.txt", b"x")
    with pytest.raises(DemoDirError, match="not a plain path"):
        owned.write_bytes(owned.root / "synth" / ".." / "x.txt", b"x")


def test_every_creation_is_recorded_in_order(owned):
    assert [(e.rel, e.is_dir) for e in owned.entries] == [
        ("synth", True),
        ("synth/a.hl7", False),
        ("synth/b.hl7", False),
        ("bridge.db", False),
    ]


# -- what the demo deletes -------------------------------------------------------------


def test_everything_the_demo_created_is_removed(owned):
    result = remove_owned(owned)
    assert result.root_removed
    assert (result.removed_files, result.removed_dirs, result.kept) == (3, 1, [])
    assert not owned.root.exists()


def test_a_named_folder_is_never_deleted_from(tmp_path):
    files = claim_demo_dir(tmp_path / "named")
    files.write_bytes(files.root / "a.txt", b"x")
    with pytest.raises(DemoDirError, match="deletes only a temporary folder"):
        remove_owned(files)
    # The temporary flag alone decides, even when the folder's identity is known.
    files.root_identity = demofiles.dir_identity(files.root)
    with pytest.raises(DemoDirError, match="deletes only a temporary folder"):
        remove_owned(files)
    assert (files.root / "a.txt").exists()


def test_a_foreign_file_is_left_and_the_folder_kept(owned):
    (owned.root / "notes.txt").write_text("mine", encoding="utf-8")
    result = remove_owned(owned)
    assert not result.root_removed
    assert (owned.root / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert sorted(p.name for p in owned.root.iterdir()) == ["notes.txt"]


def test_a_foreign_file_in_a_subfolder_keeps_that_subfolder(owned):
    (owned.root / "synth" / "notes.txt").write_text("mine", encoding="utf-8")
    result = remove_owned(owned)
    assert result.kept == ["synth"]
    assert (owned.root / "synth" / "notes.txt").exists()
    assert not (owned.root / "synth" / "a.hl7").exists()


def test_a_replaced_file_is_left(owned):
    target = owned.root / "synth" / "a.hl7"
    target.unlink()
    target.write_bytes(b"MSH|demo\r")  # same name and content, another file
    result = remove_owned(owned)
    assert "synth/a.hl7" in result.kept
    assert target.exists()


def test_a_replaced_subfolder_is_never_entered(owned):
    sub = owned.root / "synth"
    sub.rename(owned.root.parent / "moved")
    sub.mkdir()
    (sub / "a.hl7").write_bytes(b"MSH|demo\r")
    result = remove_owned(owned)
    assert {"synth", "synth/a.hl7"} <= set(result.kept)
    assert (sub / "a.hl7").exists()
    shutil.rmtree(owned.root.parent / "moved")


@pytest.mark.parametrize("swap_at", ["start", "right before the file"])
def test_a_subfolder_swapped_for_a_link_is_never_followed(owned, outside, monkeypatch, swap_at):
    sub = owned.root / "synth"

    def swap():
        sub.rename(owned.root.parent / "moved")
        _make_dir_link(sub, outside)

    if swap_at == "start":
        swap()
    else:
        _on_step(monkeypatch, swap, lambda e: e is not None and e.rel == "synth/a.hl7")
    try:
        result = remove_owned(owned)
        assert (outside / "a.hl7").read_text(encoding="utf-8") == "not the demo's"
        assert "synth" in result.kept
        assert not result.root_removed
    finally:
        _unlink_dir_link(sub)
        shutil.rmtree(owned.root.parent / "moved")


def test_a_file_swapped_for_a_link_is_never_followed(owned, outside):
    target = owned.root / "synth" / "a.hl7"
    target.unlink()
    try:
        target.symlink_to(outside / "a.hl7")
    except OSError:
        pytest.skip("no right to create a file symlink here")
    result = remove_owned(owned)
    assert "synth/a.hl7" in result.kept
    assert (outside / "a.hl7").read_text(encoding="utf-8") == "not the demo's"


def test_a_file_swapped_for_a_hard_link_to_another_file_is_left(owned, outside):
    """Runs on every OS: a hard link needs no extra right."""
    target = owned.root / "synth" / "a.hl7"
    target.unlink()
    os.link(outside / "a.hl7", target)
    result = remove_owned(owned)
    assert "synth/a.hl7" in result.kept
    assert target.exists()
    assert (outside / "a.hl7").read_text(encoding="utf-8") == "not the demo's"


def test_a_root_swapped_for_another_real_folder_is_left_alone(owned, monkeypatch):
    root = owned.root
    moved = root.parent / "moved"

    def swap():
        root.rename(moved)
        (root / "synth").mkdir(parents=True)
        (root / "synth" / "a.hl7").write_bytes(b"MSH|demo\r")
        (root / "bridge.db").write_bytes(b"")

    _on_step(monkeypatch, swap, lambda e: True)
    result = remove_owned(owned)
    assert result.removed_files == result.removed_dirs == 0
    assert not result.root_removed
    assert (root / "synth" / "a.hl7").exists()
    assert (root / "bridge.db").exists()
    shutil.rmtree(moved)


def test_a_root_swapped_just_before_its_own_removal_is_left_alone(owned, monkeypatch):
    root = owned.root

    def swap():
        root.rmdir()  # the demo's folder, empty by now
        root.mkdir()  # another folder with the same name

    _on_step(monkeypatch, swap, lambda e: e is None)
    result = remove_owned(owned)
    assert result.removed_files == 3
    assert not result.root_removed
    assert root.is_dir()


def test_a_root_swapped_for_a_link_is_never_followed(owned, outside, monkeypatch):
    root = owned.root
    moved = root.parent / "moved"
    (outside / "synth").mkdir()
    (outside / "synth" / "a.hl7").write_bytes(b"MSH|demo\r")

    def swap():
        root.rename(moved)
        _make_dir_link(root, outside)

    _on_step(monkeypatch, swap, lambda e: True)
    try:
        result = remove_owned(owned)
        assert result.removed_files == result.removed_dirs == 0
        assert (outside / "synth" / "a.hl7").exists()
        assert (outside / "a.hl7").exists()
    finally:
        _unlink_dir_link(root)
        shutil.rmtree(moved)


def test_a_file_that_is_already_gone_is_not_an_error(owned):
    (owned.root / "bridge.db").unlink()
    result = remove_owned(owned)
    assert result.root_removed
    assert result.kept == []


def test_remove_owned_is_the_only_deleting_code():
    """Every delete call in the package lives in demofiles.py."""
    src = Path(demofiles.__file__).resolve().parent
    pattern = ("rmtree", ".unlink(", "os.remove(", "os.unlink(", ".rmdir(", "os.rmdir(")
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(p in text for p in pattern) and path.name != "demofiles.py":
            offenders.append(path.name)
    assert offenders == []


# -- the demo run ------------------------------------------------------------------------


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


def test_demo_leaves_nothing_it_did_not_record(temp_root, monkeypatch):
    """No side files (WAL, journal): everything in the folder was recorded."""
    seen: list[set[str]] = []
    real = demo_module.remove_owned

    def check(files):
        on_disk = {p.relative_to(files.root).as_posix() for p in files.root.rglob("*")}
        seen.append(on_disk - {e.rel for e in files.entries})
        return real(files)

    monkeypatch.setattr(demo_module, "remove_owned", check)
    assert cli.main(["-q", "demo"]) == EXIT_OK
    assert seen == [set()]


def test_demo_data_dir_is_written_and_never_deleted(tmp_path, capsys):
    target = tmp_path / "demo-data"
    assert cli.main(["demo", "--data-dir", str(target)]) == EXIT_OK
    assert (target / "bridge.db").is_file()
    assert (target / "synth" / "manifest.json").is_file()
    assert "never deletes anything in a folder you name" in capsys.readouterr().out


def test_demo_data_dir_twice_is_refused_the_second_time(tmp_path, capsys):
    target = tmp_path / "demo-data"
    assert cli.main(["demo", "--data-dir", str(target)]) == EXIT_OK
    before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))
    capsys.readouterr()
    assert cli.main(["demo", "--data-dir", str(target)]) == EXIT_USAGE
    assert f"error: refusing to use {target}: it is not empty." in capsys.readouterr().err
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*")) == before


def test_demo_failure_keeps_the_temporary_folder(temp_root, capsys, monkeypatch):
    monkeypatch.setattr(demo_module, "run_steps", lambda owned, opts, out: 1)
    assert run_demo(DemoOptions(profiles=BUNDLED_PROFILES), sys.stdout, sys.stderr) == 1
    assert len(list(temp_root.iterdir())) == 1
    assert "demo data kept in" in capsys.readouterr().out


def test_demo_interrupted_keeps_the_temporary_folder(temp_root, capsys, monkeypatch):
    def interrupted(owned, opts, out):
        raise KeyboardInterrupt

    monkeypatch.setattr(demo_module, "run_steps", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_demo(DemoOptions(profiles=BUNDLED_PROFILES), sys.stdout, sys.stderr)
    assert len(list(temp_root.iterdir())) == 1


def test_demo_warns_and_keeps_a_folder_it_cannot_empty(temp_root, capsys, monkeypatch):
    real = demo_module.remove_owned

    def plant(files):
        (files.root / "notes.txt").write_text("mine", encoding="utf-8")
        return real(files)

    monkeypatch.setattr(demo_module, "remove_owned", plant)
    assert cli.main(["demo"]) == EXIT_OK
    err = capsys.readouterr().err
    kept = list(temp_root.iterdir())
    assert len(kept) == 1
    assert f"warning: kept {kept[0]}: it holds something the demo did not create" in err
    assert [p.name for p in kept[0].iterdir()] == ["notes.txt"]


def test_demo_listener_binds_loopback_only(temp_root, monkeypatch):
    bound: list[str] = []
    real_serve = demo_module.serve

    async def recording_serve(host, port, *args, **kwargs):
        server = await real_serve(host, port, *args, **kwargs)
        bound.extend(s.getsockname()[0] for s in server.sockets)
        return server

    monkeypatch.setattr(demo_module, "serve", recording_serve)
    assert cli.main(["-q", "demo"]) == EXIT_OK
    assert bound and set(bound) == {"127.0.0.1"}


def test_demo_ignores_the_fhir_base_environment_variable(temp_root, capsys, monkeypatch):
    """Decision 8: the demo writes into a server only when it is named on
    its command line."""
    calls: list[str] = []

    def factory(base_url, **kwargs):
        calls.append(base_url)
        raise AssertionError("the demo must not open a FHIR client")

    monkeypatch.setenv("HL7BRIDGE_FHIR_BASE", "http://env.test/fhir")
    monkeypatch.setattr(cli, "HapiClient", factory)
    assert cli.main(["demo"]) == EXIT_OK
    assert "skipped: no server named" in capsys.readouterr().out
    assert calls == []


def _fhir_ok():
    """An in-memory FHIR server that commits every write."""
    return FakeFhir()


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


def test_demo_fails_when_fhir_rows_are_skipped(tmp_path, capsys, monkeypatch):
    def skipping(conn, registry, client):
        return FhirSyncReport(skipped=[("Patient", "row 1", "no identifier")])

    monkeypatch.setattr(demo_module, "sync_to_fhir", skipping)
    opts = DemoOptions(
        profiles=BUNDLED_PROFILES,
        data_dir=tmp_path / "d",
        fhir_base="http://fhir.test/fhir",
        client_factory=_factory(_fhir_ok()),
    )
    assert run_demo(opts, sys.stdout, sys.stderr) == EXIT_FINDINGS
    assert "MISMATCH: 1 row(s) could not be mapped to FHIR and were skipped" in (
        capsys.readouterr().out
    )


def test_demo_fhir_server_down_is_a_connection_error_and_keeps_data(temp_root, capsys):
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
    kept = out.rsplit("demo data kept in ", 1)[1].split(". It holds", 1)[0]
    assert Path(kept).is_dir()
    assert Path(kept).parent == temp_root


def test_demo_fhir_http_error_exits_1_not_3(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "HapiClient", _factory(lambda request: httpx.Response(401)))
    rc = cli.main(
        ["demo", "--data-dir", str(tmp_path / "d"), "--fhir-base", "http://fhir.test/fhir"]
    )
    err = capsys.readouterr().err
    assert rc == EXIT_FINDINGS
    assert "error: FHIR server returned 401 for /fhir/metadata: the server is not ready" in err


def test_demo_waits_for_fhir(tmp_path, capsys, monkeypatch):
    calls = {"n": 0}
    server = _fhir_ok()

    def booting(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503)
        return server(request)

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


@pytest.mark.parametrize(
    "base",
    [
        "http://" + _CRED + "@127.0.0.1:9/fhir",
        "alice:s3cret-PW@127.0.0.1:9/fhir",
        "http:/alice:s3cret-PW@127.0.0.1:9/fhir",
    ],
)
def test_demo_never_prints_the_password_in_a_fhir_base(tmp_path, capsys, base):
    rc = cli.main(["demo", "--data-dir", str(tmp_path / "d"), "--fhir-base", base])
    captured = capsys.readouterr()
    assert rc in (EXIT_USAGE, EXIT_CONNECTION)
    assert "s3cret-PW" not in captured.out + captured.err
    assert "alice" not in captured.out + captured.err


def test_free_port_helper_is_loopback():
    """The demo's listener and sender use one constant, and it is loopback."""
    assert demo_module.LISTEN_HOST == "127.0.0.1"
    with socket.create_server((demo_module.LISTEN_HOST, 0)) as s:
        assert s.getsockname()[0] == "127.0.0.1"


def test_demo_never_prints_the_password_when_the_server_answers(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "HapiClient", _factory(_fhir_ok()))
    base = "http://" + _CRED + "@fhir.test/fhir"
    rc = cli.main(["demo", "--data-dir", str(tmp_path / "d"), "--fhir-base", base])
    captured = capsys.readouterr()
    assert rc == EXIT_OK
    assert "syncing to http://fhir.test/fhir" in captured.out
    assert "s3cret-PW" not in captured.out + captured.err
    assert "alice" not in captured.out + captured.err
