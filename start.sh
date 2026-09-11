#!/usr/bin/env bash
# Start the scanner web UI. Uses ./.venv if present, else the system python3.
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="$(command -v python3)"
fi

exec "$PY" app.py
