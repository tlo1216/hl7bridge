#!/usr/bin/env bash
# Deprecated: use `hl7bridge demo`. This script goes away in 0.4.0.
#
# It used to start a HAPI container, keep its data in data/demo and delete
# that folder on the next run. It now runs `hl7bridge demo` and nothing
# else: no Docker, and no deleting. The demo makes its own temporary folder
# and removes only what it created there. Arguments are passed through:
#
#   ./scripts/demo.sh
#   ./scripts/demo.sh --keep
#   ./scripts/demo.sh --fhir-base http://127.0.0.1:8080/fhir
#
# For the demo with HAPI in containers:
#   docker compose -f compose.demo.yaml up --build --exit-code-from demo
set -euo pipefail

echo "demo.sh: deprecated, removed in 0.4.0. Running hl7bridge demo instead." >&2
if command -v uv >/dev/null 2>&1; then
    exec uv run hl7bridge demo "$@"
fi
exec hl7bridge demo "$@"
