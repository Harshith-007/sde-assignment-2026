#!/usr/bin/env bash
set -euo pipefail
SIM_URL="${1:-${SIM_URL:-http://127.0.0.1:8700}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PYTHONUNBUFFERED=1
export CONTROLLER_DB="${CONTROLLER_DB:-$ROOT/controller.sqlite3}"
exec python controller.py --url "$SIM_URL" --db "$CONTROLLER_DB"
