#!/bin/bash
# Run build.sh for every pdfs/*.pdf. Output per PDF: output/<stem>.gcode
#
# Args:
#   $1 = extract mode: raster (default) | vector
#   $2 = page order: sequential (default) | spread
#
# Env: same as build.sh, except START_PAGE > 1 is rejected (use build.sh for resume).
#
# Usage:
#   ./scripts/build_all.sh
#   ./scripts/build_all.sh raster spread

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "${START_PAGE:-1}" != "1" ]]; then
  echo "ERROR: START_PAGE not supported in build_all (use build.sh for single-PDF resume)" >&2
  exit 1
fi

EXTRACT="${1:-raster}"
PAGE_ORDER="${2:-sequential}"

shopt -s nullglob
PDFS=(pdfs/*.pdf)
if [[ ${#PDFS[@]} -eq 0 ]]; then
  echo "ERROR: no pdfs/*.pdf found" >&2
  exit 1
fi

echo ">>> build_all: ${#PDFS[@]} PDF(s), extract=$EXTRACT, page-order=$PAGE_ORDER"

for pdf in "${PDFS[@]}"; do
  echo ""
  echo "============================================================"
  echo ">>> $pdf"
  echo "============================================================"
  ./scripts/build.sh "$pdf" "" "$EXTRACT" "$PAGE_ORDER"
done

echo ""
echo "✅ build_all done: ${#PDFS[@]} PDF(s) → output/"
