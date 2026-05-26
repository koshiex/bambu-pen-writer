#!/bin/bash
# Full pipeline with experimental pen post-process enabled.
#
# Same args as build.sh:
#   $1 = PDF (default: pdfs/2.pdf)
#   $2 = output G-code (default: output/<stem>_experimental[_from<N>].gcode)
#   $3 = extract mode: raster (default) | vector
#
# Always sets:
#   PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_PRESSURE=1
#   PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_FEEDRATE=1
#   PDF_TO_PRINT_EXPERIMENTAL_STRIKETHROUGH=1
#   PDF_TO_PRINT_PAGE_ORDER=spread
#   PDF_TO_PRINT_SOFT_HOLDER=1  (KEV spring holder; svg_to_gcode --soft-holder)
#
# Optional tuning (passed through if already set in the environment):
#   PDF_TO_PRINT_EXPERIMENTAL_PRESSURE_Z_RANGE_MM
#   PDF_TO_PRINT_EXPERIMENTAL_STRIKE_PROBABILITY
#   PDF_TO_PRINT_EXPERIMENTAL_RNG_SEED
#   START_PAGE  — resume from page N (default 1)
#   … see docs/pipeline.md
#
# Usage:
#   ./scripts/build_experimental.sh
#   ./scripts/build_experimental.sh pdfs/2.pdf
#   START_PAGE=9 ./scripts/build_experimental.sh pdfs/2.pdf

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_PRESSURE=1
export PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_FEEDRATE=1
export PDF_TO_PRINT_EXPERIMENTAL_STRIKETHROUGH=1
export PDF_TO_PRINT_PAGE_ORDER=spread
export PDF_TO_PRINT_SOFT_HOLDER=1
export OUT_SUFFIX=_experimental

PDF="${1:-pdfs/2.pdf}"
EXTRACT="${3:-raster}"

echo ">>> build_experimental: pressure + feedrate + strikethrough, spread, soft-holder"
echo "    PDF=$PDF  extract=$EXTRACT"
echo ""

exec ./scripts/build.sh "$PDF" "${2:-}" "$EXTRACT" spread
