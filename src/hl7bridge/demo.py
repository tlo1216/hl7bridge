"""``hl7bridge demo``: the whole pipeline on synthetic data, in one process.

The demo owns its data folder
-----------------------------

By default the demo makes a fresh folder with ``tempfile.mkdtemp`` and
removes it at the end. ``--data-dir PATH`` is accepted only when the path
does not exist yet, is an empty folder, or holds the ``.hl7bridge-demo``
marker an earlier demo wrote. The marker is JSON. It names this tool, the
device the folder lives on, and every file and folder the demo wrote, in
the order it wrote them.

``remove_demo_dir`` is the only code in hl7bridge that deletes anything.
It refuses, and deletes nothing, unless all of these hold:

- the path has no ``..`` component;
- the path is a real folder, not a symlink or a junction;
- the marker is a regular file, is valid JSON and names this tool;
- the folder is not a drive or filesystem root, and is neither the same
  folder as nor a parent of your home folder, the current folder or the
  repository root (compared by file identity, so another spelling of the
  same folder is refused too);
- the folder is on the device the marker recorded.

It then deletes only the files the marker lists, then the folders the
marker lists if they are empty, then the marker, then the folder itself if
the demo created it and it is empty. Anything else in the folder is left
in place and reported. It never follows a symlink: on systems with
``dir_fd`` support (Linux, macOS) every step works on an open folder
handle opened with ``O_NOFOLLOW``; elsewhere (Windows) the identity of
every folder on the way down is checked again right before each delete,
and a folder that turned into a link is refused.

The listener binds 127.0.0.1 on a free port. The FHIR step runs only
against a server named with ``--fhir-base``; the demo never writes into a
server you did not name, and never starts Docker.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

from . import __version__
from .acks import render_ack_text, track_acks
from .config import HospitalRegistry
from .errors import DemoDirError, NetworkError
from .fhir import HapiClient, render_sync_text, sync_to_fhir
from .ingest import IngestPipeline, send_messages, serve
from .ingest.loaders import load_outbound, load_studies
from .ingest.parser import get_field, parse_hl7
from .lint import lint_directory, render_lint_text
from .logsafety import safe_url
from .reconcile import reconcile, render_reconcile_text
from .store import connect, message_counts, quarantine_summary
from .synth import generate, verify_manifest
from .synth.manifest import Manifest
from .tat import render_tat_text, tat_report

MARKER = ".hl7bridge-demo"
TOOL = "hl7bridge"
TEMP_PREFIX = "hl7bridge-demo-"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIR_FD = (
    os.open in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


# -- ownership ------------------------------------------------------------------


def _is_link(st: os.stat_result) -> bool:
    """True for a symlink, and on Windows for any reparse point (junction)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _is_real_dir(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode) and not _is_link(st)


def _refuse(path: Path | str, why: str, hint: str = "") -> DemoDirError:
    return DemoDirError(f"refusing to use {path}: {why}", hint=hint)


def _check_no_dotdot(path: Path) -> None:
    if ".." in path.parts:
        raise _refuse(path, "it has a '..' component; give a plain path")


def _repo_root() -> Path | None:
    """The git work tree holding the current folder, if any."""
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _source_root() -> Path | None:
    """The source checkout hl7bridge runs from, for editable installs."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def protected_dirs() -> list[Path]:
    """Folders the demo must never delete, nor any parent of them."""
    found: list[Path] = [Path.home(), Path.cwd()]
    for extra in (_repo_root(), _source_root()):
        if extra is not None:
            found.append(extra)
    return found


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _check_not_protected(path: Path, real: Path) -> None:
    if real.parent == real or real == Path(real.anchor):
        raise _refuse(path, "it is a filesystem or drive root")
    for base in protected_dirs():
        try:
            resolved = base.resolve()
        except OSError:
            continue
        for candidate in (resolved, *resolved.parents):
            if _same(candidate, real):
                raise _refuse(
                    path,
                    "it is, or holds, your home folder, the current folder or the repository root",
                    hint="Pass --data-dir with a folder of its own, or leave it out.",
                )


def read_marker(path: Path) -> dict[str, Any]:
    """The parsed marker in ``path``. Raises DemoDirError when it is missing,
    is not a regular file, is not JSON, or names another tool."""
    marker = path / MARKER
    try:
        st = os.lstat(marker)
    except FileNotFoundError:
        raise _refuse(
            path,
            f"it has no {MARKER} marker, so the demo did not create it",
            hint="Remove it yourself, or pass --data-dir with a new folder.",
        ) from None
    if not stat.S_ISREG(st.st_mode) or _is_link(st):
        raise _refuse(path, f"its {MARKER} marker is not a regular file")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _refuse(
            path,
            f"its {MARKER} marker was not written by hl7bridge demo"
            " (scripts/demo.sh writes a text marker)",
            hint="Remove it yourself, or pass --data-dir with a new folder.",
        ) from None
    if not isinstance(data, dict) or data.get("tool") != TOOL:
        raise _refuse(path, f"its {MARKER} marker names another tool")
    files, dirs = data.get("files", []), data.get("dirs", [])
    if not isinstance(files, list) or not isinstance(dirs, list):
        raise _refuse(path, f"its {MARKER} marker is damaged")
    if not all(isinstance(x, str) for x in (*files, *dirs)):
        raise _refuse(path, f"its {MARKER} marker is damaged")
    if not isinstance(data.get("device"), int):
        raise _refuse(path, f"its {MARKER} marker is damaged")
    return data


@dataclass(slots=True)
class DemoDir:
    """A data folder the demo owns, and the record of what it wrote."""

    path: Path
    created_dir: bool
    device: int
    files: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)

    def save(self) -> None:
        payload = {
            "tool": TOOL,
            "note": "Created by hl7bridge demo. Synthetic data only. Safe to delete.",
            "version": __version__,
            "created_dir": self.created_dir,
            "device": self.device,
            "dirs": self.dirs,
            "files": self.files,
        }
        marker = self.path / MARKER
        try:
            st = os.lstat(marker)
        except FileNotFoundError:
            st = None
        if st is not None and (not stat.S_ISREG(st.st_mode) or _is_link(st)):
            raise _refuse(self.path, f"its {MARKER} marker was replaced by something else")
        mode = "w" if st is not None else "x"
        with open(marker, mode, encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def mkdir(self, rel: str) -> Path:
        """Create a subfolder the demo owns (exclusive, so a folder that
        appeared meanwhile is not adopted) and record it."""
        target = self.path / rel
        target.mkdir()
        self.dirs.append(rel)
        self.save()
        return target

    def record_tree(self, rel: str) -> None:
        """Record every file and folder under ``rel``, a subfolder the demo
        created itself and wrote into. Links are never recorded."""
        root = self.path / rel
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(dirpath)
            for name in sorted(dirnames):
                sub = base / name
                if not _is_link(os.lstat(sub)):
                    self.dirs.append(sub.relative_to(self.path).as_posix())
            for name in sorted(filenames):
                child = base / name
                if stat.S_ISREG(os.lstat(child).st_mode):
                    self.files.append(child.relative_to(self.path).as_posix())
        self.save()

    def record_files(self, *rels: str) -> None:
        for rel in rels:
            child = self.path / rel
            if child.exists() and rel not in self.files:
                self.files.append(rel)
        self.save()


def claim_demo_dir(path: Path | None) -> DemoDir:
    """A data folder the demo may write into, marked as owned.

    None makes a fresh temporary folder. A given path must not exist yet, or
    be an empty real folder, or hold a marker from an earlier demo (whose
    files are removed first through ``remove_demo_dir``)."""
    if path is None:
        made = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        owned = DemoDir(made, created_dir=True, device=os.lstat(made).st_dev)
        owned.save()
        return owned

    _check_no_dotdot(path)
    try:
        st: os.stat_result | None = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir()  # exclusive: fails if anything appeared meanwhile
        except FileExistsError:
            raise _refuse(path, "it appeared while the demo was starting") from None
        st = os.lstat(path)
        if not _is_real_dir(st):
            raise _refuse(path, "it is not a real folder")
        _check_not_protected(path, path.resolve())
        owned = DemoDir(path, created_dir=True, device=st.st_dev)
        owned.save()
        return owned

    if _is_link(st):
        raise _refuse(path, "it is a symlink or junction")
    if not stat.S_ISDIR(st.st_mode):
        raise _refuse(path, "it exists and is not a folder")
    _check_not_protected(path, path.resolve())
    if not any(path.iterdir()):
        owned = DemoDir(path, created_dir=False, device=st.st_dev)
        owned.save()
        return owned

    previous = read_marker(path)
    result = remove_demo_dir(path, keep_root=True)
    if result.left:
        raise _refuse(
            path,
            f"{len(result.left)} item(s) the demo did not write are still in it",
            hint="Move them out, or pass --data-dir with a new folder.",
        )
    owned = DemoDir(path, created_dir=bool(previous.get("created_dir")), device=st.st_dev)
    owned.save()
    return owned


# -- the one deleting function ---------------------------------------------------


@dataclass(slots=True)
class RemovalResult:
    removed_files: int = 0
    removed_dirs: int = 0
    root_removed: bool = False
    left: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def _between_check_and_delete() -> None:
    """No-op hook. Tests replace it to swap a folder for a link after the
    checks pass and before anything is deleted."""


def _safe_parts(rel: str) -> tuple[str, ...] | None:
    """The components of a marker entry, or None if it could escape."""
    if not rel or "\\" in rel:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute() or any(p in ("", ".", "..") for p in pure.parts):
        return None
    if ":" in pure.parts[0]:  # a Windows drive letter
        return None
    return pure.parts


class _Walker:
    """Removes entries below one root without following links."""

    def __init__(self, root: Path, identity: tuple[int, int]) -> None:
        self.root = root
        self.identity = identity

    def _check_root(self) -> None:
        st = os.lstat(self.root)
        if not _is_real_dir(st) or (st.st_dev, st.st_ino) != self.identity:
            raise DemoDirError(f"{self.root} changed while it was being removed; stopped")

    # Path based, with identity checks right before each step (Windows).
    def _parent_path(self, parts: tuple[str, ...]) -> Path | None:
        self._check_root()
        current = self.root
        for part in parts[:-1]:
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                return None
            if not _is_real_dir(st) or st.st_dev != self.identity[0]:
                raise _LinkFound(current)
        return current

    def _remove_by_path(self, parts: tuple[str, ...], *, is_dir: bool) -> bool:
        parent = self._parent_path(parts)
        if parent is None:
            return False
        target = parent / parts[-1]
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return False
        if is_dir:
            if not _is_real_dir(st):
                raise _LinkFound(target)
            target.rmdir()  # never recursive; fails if not empty
        else:
            if not stat.S_ISREG(st.st_mode) or _is_link(st):
                raise _LinkFound(target)
            target.unlink()
        return True

    # Handle based (Linux, macOS): every folder opened with O_NOFOLLOW.
    def _remove_by_fd(self, parts: tuple[str, ...], *, is_dir: bool) -> bool:
        # getattr: these flags do not exist on Windows, where _DIR_FD is False.
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fds: list[int] = []
        try:
            try:
                fd = os.open(self.root, flags)
            except OSError:
                raise DemoDirError(
                    f"{self.root} changed while it was being removed; stopped"
                ) from None
            fds.append(fd)
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != self.identity:
                raise DemoDirError(f"{self.root} changed while it was being removed; stopped")
            for part in parts[:-1]:
                try:
                    fd = os.open(part, flags, dir_fd=fds[-1])
                except FileNotFoundError:
                    return False
                except OSError:
                    raise _LinkFound(self.root.joinpath(*parts)) from None
                fds.append(fd)
                if os.fstat(fd).st_dev != self.identity[0]:
                    raise _LinkFound(self.root.joinpath(*parts))
            name = parts[-1]
            try:
                st = os.stat(name, dir_fd=fds[-1], follow_symlinks=False)
            except FileNotFoundError:
                return False
            if is_dir:
                if not _is_real_dir(st):
                    raise _LinkFound(self.root.joinpath(*parts))
                os.rmdir(name, dir_fd=fds[-1])
            else:
                if not stat.S_ISREG(st.st_mode):
                    raise _LinkFound(self.root.joinpath(*parts))
                os.unlink(name, dir_fd=fds[-1])
            return True
        finally:
            for opened in reversed(fds):
                os.close(opened)

    def remove(self, parts: tuple[str, ...], *, is_dir: bool) -> bool:
        if _DIR_FD:
            return self._remove_by_fd(parts, is_dir=is_dir)
        return self._remove_by_path(parts, is_dir=is_dir)


class _LinkFound(Exception):  # an internal signal; never leaves this module
    def __init__(self, where: Path) -> None:
        super().__init__(str(where))
        self.where = where


def remove_demo_dir(path: Path, *, keep_root: bool = False) -> RemovalResult:
    """Delete what the demo wrote in ``path``, and nothing else.

    See the module docstring for every check. Raises DemoDirError, having
    deleted nothing, when a check fails. ``keep_root`` leaves the folder
    itself in place (used when a demo reuses its folder)."""
    _check_no_dotdot(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise _refuse(path, "it does not exist") from None
    if _is_link(st):
        raise _refuse(path, "it is a symlink or junction")
    if not stat.S_ISDIR(st.st_mode):
        raise _refuse(path, "it is not a folder")
    real = path.resolve(strict=True)
    _check_not_protected(path, real)
    marker = read_marker(path)
    if marker["device"] != st.st_dev:
        raise _refuse(path, "it is not on the device the demo created it on")

    files: list[tuple[str, tuple[str, ...]]] = []
    dirs: list[tuple[str, tuple[str, ...]]] = []
    result = RemovalResult()
    for rel in marker["files"]:
        parts = _safe_parts(rel)
        if parts is None:
            result.refused.append(rel)
        else:
            files.append((rel, parts))
    for rel in marker["dirs"]:
        parts = _safe_parts(rel)
        if parts is None:
            result.refused.append(rel)
        else:
            dirs.append((rel, parts))

    _between_check_and_delete()

    walker = _Walker(path, (st.st_dev, st.st_ino))
    for rel, parts in files:
        try:
            if walker.remove(parts, is_dir=False):
                result.removed_files += 1
        except _LinkFound:
            result.refused.append(rel)
    for rel, parts in sorted(dirs, key=lambda item: len(item[1]), reverse=True):
        try:
            if walker.remove(parts, is_dir=True):
                result.removed_dirs += 1
        except _LinkFound:
            result.refused.append(rel)
        except OSError:
            pass  # not empty: something the demo did not write is inside
    try:
        walker.remove((MARKER,), is_dir=False)
    except _LinkFound:
        result.refused.append(MARKER)

    result.left = sorted(p.name for p in path.iterdir())
    if not keep_root and marker.get("created_dir") and not result.left:
        walker._check_root()
        path.rmdir()
        result.root_removed = True
    return result


# -- the pipeline --------------------------------------------------------------


@dataclass(slots=True)
class DemoOptions:
    profiles: Path
    data_dir: Path | None = None
    keep: bool = False
    seed: int = 42
    fhir_base: str | None = None
    fhir_wait_s: float = 0.0
    fhir_headers: dict[str, str] = field(default_factory=dict)
    client_factory: Callable[..., HapiClient] = HapiClient


def _section(out: TextIO, title: str) -> None:
    print(f"\n== {title} ==", file=out)


def _wait_for_fhir(client: HapiClient, base: str, wait_s: float) -> None:
    deadline = time.monotonic() + wait_s
    while True:
        status = client.metadata_status()
        if status == 200:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(3)
    if status == 0:
        raise NetworkError(
            f"no answer from {safe_url(base)}/metadata. Is the server running?",
            hint="Try: hl7bridge doctor",
        )
    raise NetworkError(f"FHIR server at {safe_url(base)} answered /metadata with HTTP {status}.")


def _replay(pipeline: IngestPipeline, payloads: list[bytes]) -> list[bytes]:
    async def run() -> list[bytes]:
        server = await serve("127.0.0.1", 0, pipeline.handle_frame, pipeline.handle_junk)
        port = server.sockets[0].getsockname()[1]
        try:
            return await send_messages("127.0.0.1", port, payloads)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(run())


def run_steps(owned: DemoDir, opts: DemoOptions, out: TextIO) -> int:
    """Run the demo in ``owned``. Returns 0 when every count matches the
    manifest, 1 otherwise."""
    registry = HospitalRegistry.load_dir(opts.profiles)
    synth_dir = owned.mkdir("synth")

    _section(out, f"1. Generate a synthetic day (seed {opts.seed})")
    manifest = generate(synth_dir, registry, seed=opts.seed)
    owned.record_tree("synth")
    problems = verify_manifest(synth_dir, registry)
    print(f"hospitals: {', '.join(manifest.hospitals)}", file=out)
    print(f"seeded defects: {len(manifest.defects)}", file=out)
    if problems:
        print(f"manifest check: {len(problems)} mismatch(es)", file=out)
        return 1
    print("manifest checked against the generated files", file=out)

    _section(out, "2. Replay the day over MLLP (listener on 127.0.0.1, free port)")
    db_path = owned.path / "bridge.db"
    conn = connect(db_path)
    owned.record_files("bridge.db", "bridge.db-wal", "bridge.db-shm")
    try:
        status = _run_store_steps(opts, out, registry, conn, synth_dir, manifest)
    finally:
        conn.close()
        owned.record_files("bridge.db", "bridge.db-wal", "bridge.db-shm")
    return status


def _run_store_steps(
    opts: DemoOptions,
    out: TextIO,
    registry: HospitalRegistry,
    conn: sqlite3.Connection,
    synth_dir: Path,
    manifest: Manifest,
) -> int:
    pipeline = IngestPipeline(registry, conn)
    payloads = [p.read_bytes() for p in sorted((synth_dir / "messages").rglob("*.hl7"))]
    acks = _replay(pipeline, payloads)
    codes: Counter[str] = Counter()
    for ack in acks:
        codes[get_field(parse_hl7(ack.decode("utf-8")), "MSA-1") or "??"] += 1
    print(f"sent {len(payloads)} messages, got {len(acks)} ACKs", file=out)
    for code in sorted(codes):
        print(f"  {code}: {codes[code]}", file=out)

    _section(out, "3. Load DICOM metadata and the outbound report log")
    counts = load_studies(conn, registry, synth_dir / "dicom")
    print(f"studies loaded: {counts['inserted']}", file=out)
    n_out = load_outbound(conn, registry, synth_dir / "outbound_reports.jsonl")
    print(f"outbound report entries loaded: {n_out}", file=out)

    now = datetime.fromisoformat(manifest.report_now_utc)
    _section(out, "4. Ingest summary (the quarantine holds exactly the seeded defects)")
    for msg_type, n in message_counts(conn).items():
        print(f"  {msg_type}: {n}", file=out)
    quarantined = quarantine_summary(conn)
    print(f"quarantine ({sum(quarantined.values())} total):", file=out)
    for reason, n in quarantined.items():
        print(f"  {reason}: {n}", file=out)

    _section(out, f"5. Reconciliation report (as of {now.isoformat()})")
    print(render_reconcile_text(reconcile(conn, registry, now)), file=out)

    _section(out, "6. Report delivery and ACK report")
    print(render_ack_text(track_acks(conn, registry, now), registry), file=out)

    _section(out, "7. Lint the raw messages (the findings are the seeded defects)")
    print(render_lint_text(lint_directory(synth_dir / "messages", registry)), file=out)

    month = now.strftime("%Y-%m")
    _section(out, f"8. Turnaround-time report for {month}")
    print(render_tat_text(tat_report(conn, registry, month)), file=out)

    _section(out, "9. FHIR")
    if opts.fhir_base is None:
        print("skipped: no server named. Add --fhir-base URL, or run the compose demo.", file=out)
    else:
        with opts.client_factory(opts.fhir_base, extra_headers=opts.fhir_headers) as client:
            _wait_for_fhir(client, opts.fhir_base, opts.fhir_wait_s)
            print(f"syncing to {safe_url(opts.fhir_base)}", file=out)
            report = sync_to_fhir(conn, registry, client)
            print(render_sync_text(report), file=out)

    _section(out, "10. Check against the manifest")
    mismatches: list[str] = []
    if quarantined != manifest.expected["quarantine"]:
        mismatches.append("quarantine counts differ from the manifest")
    if codes.get("AA", 0) != manifest.expected["messages_aa_total"]:
        mismatches.append("AA count differs from the manifest")
    if len(acks) != len(payloads):
        mismatches.append("some messages got no ACK")
    for line in mismatches:
        print(f"  MISMATCH: {line}", file=out)
    if mismatches:
        return 1
    print("every ACK and quarantine count matches the manifest", file=out)
    return 0


def run_demo(opts: DemoOptions, out: TextIO, err: TextIO) -> int:
    """Claim the data folder, run the steps, then tidy up.

    The folder is removed only after a clean run in a temporary folder
    without ``--keep``. With ``--data-dir``, ``--keep``, or after a failure
    or Ctrl+C, it stays, and its path is printed."""
    owned = claim_demo_dir(opts.data_dir)
    print(f"demo data folder: {owned.path}", file=out)
    status: int | None = None
    try:
        status = run_steps(owned, opts, out)
    finally:
        temporary = opts.data_dir is None
        if status == 0 and temporary and not opts.keep:
            result = remove_demo_dir(owned.path)
            if result.root_removed:
                print(f"\nremoved the demo data folder {owned.path}", file=out)
            if result.left or result.refused:
                print(
                    f"warning: left {len(result.left) + len(result.refused)} item(s) in"
                    f" {owned.path} that the demo did not write or could not prove it wrote",
                    file=err,
                )
        else:
            print(f"\ndemo data kept in {owned.path}", file=out)
    return status
