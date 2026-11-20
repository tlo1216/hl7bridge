#!/usr/bin/env bash
# End-to-end demo: a synthetic day for three hospitals.
#
# Prereqs: uv sync; docker (for HAPI FHIR). Run from the repo root:
#   ./scripts/demo.sh
#
# Steps: start HAPI, generate data, start the MLLP listener, replay the
# day, load DICOM metadata and the outbound log, then print the
# reconciliation report, the ACK report, the quarantine summary, and sync
# to FHIR. REPORT_NOW comes from the manifest so reports are deterministic.
#
# Safety: DATA_DIR is a disposable folder this script owns. It is deleted
# and recreated on each run only if it holds the marker file the script
# wrote (see scripts/demo_guard.sh). HAPI is published on 127.0.0.1 only.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
# shellcheck source=scripts/demo_guard.sh
. "$SCRIPT_DIR/demo_guard.sh"

DATA_DIR="${DATA_DIR:-data/demo}"
DB="$DATA_DIR/bridge.db"
SEED="${SEED:-42}"
MLLP_PORT="${MLLP_PORT:-2575}"
FHIR_BASE="${FHIR_BASE:-http://127.0.0.1:8080/fhir}"
HAPI_IMAGE="hapiproject/hapi:v8.12.0-2"

run() { uv run hl7bridge "$@"; }

# Before anything starts: claim (or safely reset) the owned data folder.
prepare_data_dir "$DATA_DIR" "$REPO_ROOT"

echo "== 0. HAPI FHIR =="
if curl -fsS "$FHIR_BASE/metadata" >/dev/null 2>&1; then
    echo "FHIR server already answering at $FHIR_BASE"
else
    docker rm -f hl7bridge-hapi >/dev/null 2>&1 || true  # stale container from a previous run
    docker run -d --name hl7bridge-hapi -p 127.0.0.1:8080:8080 "$HAPI_IMAGE"
    echo -n "waiting for HAPI to boot"
    for _ in $(seq 1 90); do
        if curl -fsS "$FHIR_BASE/metadata" >/dev/null 2>&1; then break; fi
        echo -n "."
        sleep 2
    done
    echo
    curl -fsS "$FHIR_BASE/metadata" >/dev/null # fail loudly if it never came up
fi

echo "== 1. Generate synthetic day (seed=$SEED) =="
run synth --out "$DATA_DIR/synth" --config config/hospitals --seed "$SEED"
REPORT_NOW="$(MANIFEST="$DATA_DIR/synth/manifest.json" uv run python -c 'import json,os;print(json.load(open(os.environ["MANIFEST"]))["report_now_utc"])')"

echo "== 2. Start MLLP listener =="
uv run hl7bridge serve --db "$DB" --config config/hospitals --port "$MLLP_PORT" &
LISTENER_PID=$!
trap 'kill "$LISTENER_PID" 2>/dev/null || true; wait "$LISTENER_PID" 2>/dev/null || true' EXIT
sleep 1

echo "== 3. Replay the day over MLLP =="
run send --dir "$DATA_DIR/synth/messages" --port "$MLLP_PORT"

echo "== 4. Load DICOM metadata and outbound report log =="
run load-studies --db "$DB" --config config/hospitals --dicom "$DATA_DIR/synth/dicom"
run load-outbound --db "$DB" --path "$DATA_DIR/synth/outbound_reports.jsonl"

echo "== 5. Reconciliation report (as of $REPORT_NOW) =="
run reconcile --db "$DB" --config config/hospitals --now "$REPORT_NOW"

echo "== 6. Report delivery / ACK report =="
run ack-report --db "$DB" --config config/hospitals --now "$REPORT_NOW"

echo "== 7. Ingest summary (quarantine counts are the seeded defects) =="
run summary --db "$DB"

echo "== 7b. Lint the raw message corpus (findings are the seeded defects) =="
run lint --dir "$DATA_DIR/synth/messages" --config config/hospitals || true

echo "== 7c. Turnaround-time report for 2026-10 =="
run tat --db "$DB" --config config/hospitals --month 2026-10

echo "== 8. Sync to FHIR at $FHIR_BASE =="
run fhir-sync --db "$DB" --config config/hospitals --fhir-base "$FHIR_BASE"

echo "== 9. Spot-check HAPI =="
for resource in Patient ServiceRequest DiagnosticReport ImagingStudy; do
    total=$(curl -fsS "$FHIR_BASE/$resource?_summary=count" | uv run python -c "import json,sys;print(json.load(sys.stdin)['total'])")
    echo "  $resource on server: $total"
done

echo "Demo complete. Stopping the MLLP listener; data remains in $DB."
