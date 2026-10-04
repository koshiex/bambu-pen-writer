#!/bin/bash
# Pen-plotter web UI on http://127.0.0.1:8765 (opens the browser).
#   ./scripts/webui.sh                  # real printer (settings on the «Принтер» tab)
#   ./scripts/webui.sh --demo-printer   # built-in fake P1S, accelerated time, no hardware
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="$ROOT/.venv/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON=$(command -v python3)
exec "$PYTHON" -m webui "$@"
