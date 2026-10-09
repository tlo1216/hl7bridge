#!/usr/bin/env bash
# Weekend demo: synthetic day for three hospitals, end to end.
#
# Prereqs: uv sync; docker (for HAPI FHIR). Run from the repo root:
#   ./scripts/demo.sh
#
# Steps: start HAPI, generate data, start the MLLP listener, replay the
# day, load DICOM metadata and the outbound log, then print the
# reconciliation report, the ACK report, the quarantine summary, and sync
# to FHIR. REPORT_NOW comes from the manifest so reports are deterministic.
set -euo pipefail

DATA_DIR="${DATA_DIR:-data/demo}"
DB="$DATA_DIR/bridge.db"
SEED="${SEED:-42}"
MLLP_PORT="${MLLP_PORT:-2575}"
FHIR_BASE="${FHIR_BASE:-http://localhost:8080/fhir}"
HAPI_IMAGE="hapiproject/hapi:v8.12.0-2"

run() { uv run hl7bridge "$@"; }

echo "== 0. HAPI FHIR =="
if curl -fsS "$FHIR_BASE/metadata" >/dev/null 2>&1; then
    echo "FHIR server already answering at $FHIR_BASE"
else
    docker run -d --name hl7bridge-hapi -p 8080:8080 "$HAPI_IMAGE"
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
rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR"
run synth --out "$DATA_DIR/synth" --config config/hospitals --seed "$SEED"
REPORT_NOW="$(uv run python -c "import json;print(json.load(open('$DATA_DIR/synth/manifest.json'))['report_now_utc'])")"

echo "== 2. Start MLLP listener =="
run serve --db "$DB" --config config/hospitals --port "$MLLP_PORT" &
LISTENER_PID=$!
trap 'kill "$LISTENER_PID" 2>/dev/null || true' EXIT
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

echo "== 8. Sync to FHIR at $FHIR_BASE =="
run fhir-sync --db "$DB" --config config/hospitals --fhir-base "$FHIR_BASE"

echo "== 9. Spot-check HAPI =="
for resource in Patient ServiceRequest DiagnosticReport ImagingStudy; do
    total=$(curl -fsS "$FHIR_BASE/$resource?_summary=count" | uv run python -c "import json,sys;print(json.load(sys.stdin)['total'])")
    echo "  $resource on server: $total"
done

echo "Demo complete. MLLP listener still writing to $DB; Ctrl+C handlers will stop it."
