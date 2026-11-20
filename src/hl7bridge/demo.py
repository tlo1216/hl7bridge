"""``hl7bridge demo``: the whole pipeline on synthetic data, in one process.

Where the demo writes, and what it deletes
------------------------------------------

By default the demo makes a new folder with ``tempfile.mkdtemp`` in the
system temp folder and writes everything there. Only at the end of a clean
run, and without ``--keep``, does it remove that folder, through
``hl7bridge.demofiles.remove_owned``: it deletes exactly the files and
folders it created in this run, checked against the identities recorded
when it created them, never follows a link, and leaves anything else in
place. If anything is left, the folder is kept and its path is printed.
After a failed or interrupted run the folder is kept too.

``--data-dir PATH`` writes into a new or empty folder and leaves everything
there. hl7bridge never deletes anything in a folder you name, and refuses
one that is not empty.

The listener binds 127.0.0.1 on a free port. The FHIR step runs only
against a server named with ``--fhir-base``; the demo never writes into a
server you did not name, and never starts Docker.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .acks import render_ack_text, track_acks
from .config import HospitalRegistry
from .demofiles import OwnedFiles, remove_owned
from .errors import DemoDirError, FhirPostError, NetworkError
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

TEMP_PREFIX = "hl7bridge-demo-"
LISTEN_HOST = "127.0.0.1"


def _refuse(path: Path | str, why: str, hint: str = "") -> DemoDirError:
    return DemoDirError(f"refusing to use {path}: {why}", hint=hint)


def claim_demo_dir(path: Path | None) -> OwnedFiles:
    """The folder the demo writes into.

    None makes a new temporary folder, the only kind the demo ever removes.
    A named path must not exist yet, or be an empty folder; the demo writes
    there and leaves everything in place."""
    if path is None:
        return OwnedFiles(Path(tempfile.mkdtemp(prefix=TEMP_PREFIX)), temporary=True)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        try:
            path.mkdir(parents=True)  # exclusive for the last component
        except FileExistsError:
            raise _refuse(path, "it appeared while the demo was starting") from None
        except OSError as exc:
            raise _refuse(path, f"it cannot be created ({type(exc).__name__})") from None
        return OwnedFiles(path, temporary=False)
    except OSError as exc:
        raise _refuse(path, f"it cannot be read ({type(exc).__name__})") from None
    if not stat.S_ISDIR(st.st_mode):
        raise _refuse(path, "it exists and is not a folder")
    if any(path.iterdir()):
        raise _refuse(
            path,
            "it is not empty. The demo writes only into a new or empty folder,"
            " and never deletes anything in a folder you name.",
            hint="Pass --data-dir with a new folder, or leave it out to use a temporary folder.",
        )
    return OwnedFiles(path, temporary=False)


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
    # An HTTP answer is a server problem (exit 1), not a connection error (exit 3).
    raise FhirPostError(status, f"{safe_url(base)}/metadata", reason="the server is not ready")


def _replay(pipeline: IngestPipeline, payloads: list[bytes]) -> list[bytes]:
    async def run() -> list[bytes]:
        server = await serve(LISTEN_HOST, 0, pipeline.handle_frame, pipeline.handle_junk)
        port = server.sockets[0].getsockname()[1]
        try:
            return await send_messages(LISTEN_HOST, port, payloads)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(run())


def run_steps(owned: OwnedFiles, opts: DemoOptions, out: TextIO) -> int:
    """Run the demo in ``owned``. Returns 0 when every count matches the
    manifest, 1 otherwise. Every file goes through ``owned``, so a
    temporary folder can be removed exactly."""
    registry = HospitalRegistry.load_dir(opts.profiles)
    synth_dir = owned.root / "synth"

    _section(out, f"1. Generate a synthetic day (seed {opts.seed})")
    manifest = generate(synth_dir, registry, seed=opts.seed, sink=owned)
    problems = verify_manifest(synth_dir, registry)
    print(f"hospitals: {', '.join(manifest.hospitals)}", file=out)
    print(f"seeded defects: {len(manifest.defects)}", file=out)
    if problems:
        print(f"manifest check: {len(problems)} mismatch(es)", file=out)
        return 1
    print("manifest checked against the generated files", file=out)

    _section(out, f"2. Replay the day over MLLP (listener on {LISTEN_HOST}, free port)")
    db_path = owned.root / "bridge.db"
    # The demo creates the file itself, so it is recorded; the rollback
    # journal (no WAL) leaves no side files once the connection closes.
    owned.write_bytes(db_path, b"")
    conn = connect(db_path, wal=False)
    try:
        return _run_store_steps(opts, out, registry, conn, synth_dir, manifest)
    finally:
        conn.close()


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
    skipped = 0
    if opts.fhir_base is None:
        print("skipped: no server named. Add --fhir-base URL, or run the compose demo.", file=out)
    else:
        with opts.client_factory(opts.fhir_base, extra_headers=opts.fhir_headers) as client:
            _wait_for_fhir(client, opts.fhir_base, opts.fhir_wait_s)
            print(f"syncing to {safe_url(opts.fhir_base)}", file=out)
            report = sync_to_fhir(conn, registry, client)
            print(render_sync_text(report), file=out)
            skipped = len(report.skipped)

    _section(out, "10. Check against the manifest")
    mismatches: list[str] = []
    if quarantined != manifest.expected["quarantine"]:
        mismatches.append("quarantine counts differ from the manifest")
    if codes.get("AA", 0) != manifest.expected["messages_aa_total"]:
        mismatches.append("AA count differs from the manifest")
    if len(acks) != len(payloads):
        mismatches.append("some messages got no ACK")
    if skipped:
        mismatches.append(f"{skipped} row(s) could not be mapped to FHIR and were skipped")
    for line in mismatches:
        print(f"  MISMATCH: {line}", file=out)
    if mismatches:
        return 1
    print("every ACK and quarantine count matches the manifest", file=out)
    return 0


def run_demo(opts: DemoOptions, out: TextIO, err: TextIO) -> int:
    """Pick the data folder, run the steps, then tidy up.

    Only a temporary folder, after a clean run without ``--keep``, is
    removed. A folder named with ``--data-dir`` is never touched after the
    run, and after a failure or Ctrl+C every folder stays."""
    owned = claim_demo_dir(opts.data_dir)
    print(f"demo data folder: {owned.root}", file=out)
    status: int | None = None
    try:
        status = run_steps(owned, opts, out)
    finally:
        if not owned.temporary:
            print(
                f"\ndemo data written to {owned.root}. hl7bridge never deletes anything"
                " in a folder you name.",
                file=out,
            )
        elif status == 0 and not opts.keep:
            result = remove_owned(owned)
            if result.root_removed:
                print(f"\nremoved the demo data folder {owned.root}", file=out)
            else:
                print(
                    f"warning: kept {owned.root}: it holds something the demo did not create,"
                    " or something it created was changed. Look inside, then delete it"
                    " yourself.",
                    file=err,
                )
        else:
            print(
                f"\ndemo data kept in {owned.root}. It holds synthetic data only;"
                " delete it when you are done.",
                file=out,
            )
    return status
