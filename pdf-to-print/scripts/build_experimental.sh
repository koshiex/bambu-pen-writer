#!/bin/bash
# Full pipeline with experimental pen post-process enabled.
#
# Same args as build.sh:
#   $1 = PDF (default: pdfs/2.pdf)
#   $2 = output G-code (default: output/notebook_experimental.gcode)
#   $3 = extract mode: raster (default) | vector
#
# Always sets:
#   PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_PRESSURE=1
#   PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_FEEDRATE=1
#   PDF_TO_PRINT_EXPERIMENTAL_STRIKETHROUGH=1
#   PDF_TO_PRINT_PAGE_ORDER=spread
#
# Optional tuning (passed through if already set in the environment):
#   PDF_TO_PRINT_EXPERIMENTAL_PRESSURE_Z_RANGE_MM
#   PDF_TO_PRINT_EXPERIMENTAL_STRIKE_PROBABILITY
#   PDF_TO_PRINT_EXPERIMENTAL_RNG_SEED
#   … see docs/pipeline.md
#
# Usage:
#   ./scripts/build_experimental.sh
#   ./scripts/build_experimental.sh pdfs/2.pdf output/notebook_experimental.gcode

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_PRESSURE=1
export PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_FEEDRATE=1
export PDF_TO_PRINT_EXPERIMENTAL_STRIKETHROUGH=1
export PDF_TO_PRINT_PAGE_ORDER=spread

PDF="${1:-pdfs/2.pdf}"
OUT="${2:-output/notebook_experimental.gcode}"
EXTRACT="${3:-raster}"

echo ">>> build_experimental: pressure + feedrate + strikethrough, page order=spread"
echo "    PDF=$PDF  OUT=$OUT  extract=$EXTRACT"
echo ""

exec ./scripts/build.sh "$PDF" "$OUT" "$EXTRACT" spread
