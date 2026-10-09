"""hl7bridge command-line interface.

Subcommands mirror the demo flow:
  synth -> serve -> send -> load-studies -> load-outbound ->
  reconcile / ack-report / summary -> fhir-sync
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .acks import render_ack_text, track_acks
from .config import HospitalRegistry
from .fhir import HapiClient, render_sync_text, sync_to_fhir
from .ingest import IngestPipeline, send_messages, serve
from .ingest.loaders import load_outbound, load_studies
from .ingest.parser import get_field, parse_hl7
from .lint import lint_directory, render_lint_text
from .reconcile import reconcile, render_reconcile_text
from .store import connect, message_counts, quarantine_summary
from .synth import generate, verify_manifest
from .tat import render_tat_text, tat_report

log = logging.getLogger(__name__)


def _registry(args: argparse.Namespace) -> HospitalRegistry:
    return HospitalRegistry.load_dir(Path(args.config))


def _now_arg(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise SystemExit(f"--now {value!r} must carry a UTC offset, e.g. 2026-10-08T12:00:00+00:00")
    return dt


def cmd_synth(args: argparse.Namespace) -> int:
    registry = _registry(args)
    out = Path(args.out)
    manifest = generate(out, registry, seed=args.seed)
    print(f"generated synthetic day under {out} (seed={args.seed})")
    print(f"  hospitals: {', '.join(manifest.hospitals)}")
    print(f"  seeded defects: {len(manifest.defects)}")
    problems = verify_manifest(out, registry)
    if problems:
        for p in problems:
            print(f"  MANIFEST MISMATCH: {p}", file=sys.stderr)
        return 1
    print("  manifest verified against generated artifacts")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    registry = _registry(args)
    conn = connect(args.db)
    pipeline = IngestPipeline(registry, conn)

    async def run() -> None:
        server = await serve(args.host, args.port, pipeline.handle_frame, pipeline.handle_junk)
        async with server:
            await server.serve_forever()

    print(f"MLLP listener on {args.host}:{args.port}, db {args.db} (Ctrl+C to stop)")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("listener stopped")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    paths = sorted(Path(args.dir).rglob("*.hl7"))
    if not paths:
        raise SystemExit(f"no *.hl7 files under {args.dir}")
    payloads = [p.read_bytes() for p in paths]

    acks = asyncio.run(send_messages(args.host, args.port, payloads))
    counts: Counter[str] = Counter()
    for ack_bytes in acks:
        code = get_field(parse_hl7(ack_bytes.decode("utf-8")), "MSA-1") or "??"
        counts[code] += 1
    print(f"sent {len(payloads)} messages from {args.dir}")
    for code in sorted(counts):
        print(f"  {code}: {counts[code]}")
    if len(acks) != len(payloads):
        print(f"  WARNING: {len(payloads) - len(acks)} messages got no ACK", file=sys.stderr)
        return 1
    return 0


def cmd_load_studies(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    counts = load_studies(conn, _registry(args), Path(args.dicom))
    print(f"studies loaded: inserted={counts['inserted']} replaced={counts['replaced']}")
    return 0


def cmd_load_outbound(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    n = load_outbound(conn, Path(args.path))
    print(f"outbound report entries loaded: {n}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    reports = reconcile(conn, _registry(args), _now_arg(args.now))
    print(render_reconcile_text(reports))
    return 0


def cmd_ack_report(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    registry = _registry(args)
    report = track_acks(conn, registry, _now_arg(args.now))
    print(render_ack_text(report, registry))
    return 0


def cmd_tat(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    reports = tat_report(conn, _registry(args), args.month)
    print(render_tat_text(reports))
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    report = lint_directory(Path(args.dir), _registry(args))
    print(render_lint_text(report))
    # Linter convention: errors fail the invocation (warnings do not).
    return 1 if report.error_count else 0


def cmd_fhir_sync(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    with HapiClient(args.fhir_base) as client:
        if not client.metadata_ok():
            raise SystemExit(f"FHIR server at {args.fhir_base} is not answering /metadata")
        report = sync_to_fhir(conn, _registry(args), client)
    print(render_sync_text(report))
    return 1 if report.skipped else 0


def cmd_summary(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    print("accepted messages by type:")
    for msg_type, n in message_counts(conn).items():
        print(f"  {msg_type}: {n}")
    quarantined = quarantine_summary(conn)
    total = sum(quarantined.values())
    print(f"quarantine ({total} total):")
    for reason, n in quarantined.items():
        print(f"  {reason}: {n}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hl7bridge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(
        name: str, func: Callable[[argparse.Namespace], int], help: str
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.set_defaults(func=func)
        return p

    p = add("synth", cmd_synth, help="generate a synthetic day of HL7 + DICOM traffic")
    p.add_argument("--out", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--seed", type=int, default=42)

    p = add("serve", cmd_serve, help="run the MLLP listener")
    p.add_argument("--db", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=2575)

    p = add("send", cmd_send, help="replay *.hl7 files into an MLLP listener")
    p.add_argument("--dir", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=2575)

    p = add("load-studies", cmd_load_studies, help="load DICOM study metadata")
    p.add_argument("--db", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--dicom", required=True)

    p = add("load-outbound", cmd_load_outbound, help="load the outbound report log")
    p.add_argument("--db", required=True)
    p.add_argument("--path", required=True)

    p = add("reconcile", cmd_reconcile, help="order-to-image reconciliation report")
    p.add_argument("--db", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--now", default=None, help="aware ISO timestamp; defaults to current time")

    p = add("ack-report", cmd_ack_report, help="report delivery / ACK tracking report")
    p.add_argument("--db", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--now", default=None, help="aware ISO timestamp; defaults to current time")

    p = add("tat", cmd_tat, help="monthly turnaround-time report")
    p.add_argument("--db", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--month", required=True, help="YYYY-MM, in hospital local time")

    p = add("lint", cmd_lint, help="lint *.hl7 files against hospital profiles")
    p.add_argument("--dir", required=True)
    p.add_argument("--config", required=True)

    p = add("fhir-sync", cmd_fhir_sync, help="post store contents to a FHIR server")
    p.add_argument("--db", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--fhir-base", default="http://localhost:8080/fhir")

    p = add("summary", cmd_summary, help="message and quarantine counters")
    p.add_argument("--db", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
