#!/bin/bash
# Extract PDF pages via raster → vector SVG for vpype.
#
# Default TRACE_MODE=skeleton: PNG → skeletonize → centerline polylines (one stroke,
# no inner+outer contour from potrace around thick strokes).
#
# TRACE_MODE=potrace: PNG → mkbitmap → potrace outlines (legacy; can leave double contours
# on ring-shaped strokes).
#
# Args: $1 = PDF path (default: pdfs/2.pdf)
#       $2 = output dir for SVGs (default: build/svg)
#
# Env:
#   TRACE_MODE     — skeleton (default) | potrace
#   EXPORT_DPI     — PNG DPI for inkscape (default: 300)
#   MKBITMAP_OPTS  — with potrace: mkbitmap args (default: -f 2 -s 1 -t 0.45)
#   POTRACE_OPTS   — extra potrace flags before -s (default: empty)
#
# Requires: inkscape; python3 (.venv) with numpy scikit-image networkx pillow.
# potrace mode additionally: magick|convert, potrace, mkbitmap

set -euo pipefail

PDF="${1:-pdfs/2.pdf}"
OUT_DIR="${2:-build/svg}"
EXPORT_DPI="${EXPORT_DPI:-300}"
MKBITMAP_OPTS="${MKBITMAP_OPTS:--f 2 -s 1 -t 0.45}"
POTRACE_OPTS="${POTRACE_OPTS:-}"
TRACE_MODE="${TRACE_MODE:-skeleton}"
case "$TRACE_MODE" in
  skeleton|potrace) ;;
  *)
    echo "ERROR: TRACE_MODE must be skeleton or potrace, got: $TRACE_MODE" >&2
    exit 1
    ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NORMALIZE="$ROOT/scripts/normalize_traced_svg.py"
SKELETON="$ROOT/scripts/png_to_skeleton_svg.py"
PYTHON="$ROOT/.venv/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON=$(command -v python3 || true)

if [[ ! -f "$PDF" ]]; then
  echo "ERROR: $PDF not found" >&2
  exit 1
fi

for cmd in inkscape; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: $cmd not in PATH" >&2
    exit 1
  fi
done
if [[ "$TRACE_MODE" == "potrace" ]]; then
  for cmd in potrace mkbitmap; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "ERROR: potrace mode requires $cmd in PATH" >&2
      exit 1
    fi
  done
  MAGICK=""
  if command -v magick >/dev/null 2>&1; then
    MAGICK="magick"
  elif command -v convert >/dev/null 2>&1; then
    MAGICK="convert"
  else
    echo "ERROR: potrace mode needs magick or convert (ImageMagick) for PNG→PGM" >&2
    exit 1
  fi
fi
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: python3 not found (install or create .venv)" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/p2p_raster.XXXXXX")
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# Page count: mdls (macOS) → Python regex fallback (same as extract_pages.sh)
PAGE_COUNT=""
if command -v mdls >/dev/null 2>&1; then
  PAGE_COUNT=$(mdls -name kMDItemNumberOfPages -raw "$PDF" 2>/dev/null || true)
fi

if ! [[ "$PAGE_COUNT" =~ ^[0-9]+$ ]] || [[ "$PAGE_COUNT" -lt 1 ]]; then
  PAGE_COUNT=$(python3 -c "
import re
data = open('$PDF', 'rb').read()
print(len(re.findall(rb'/Type\s*/Page[^s]', data)))
" 2>/dev/null || echo "")
fi

if ! [[ "$PAGE_COUNT" =~ ^[0-9]+$ ]] || [[ "$PAGE_COUNT" -lt 1 ]]; then
  echo "ERROR: cannot detect page count of $PDF" >&2
  exit 1
fi

echo "Raster extract: $PAGE_COUNT pages from $PDF -> $OUT_DIR (dpi=$EXPORT_DPI, TRACE_MODE=$TRACE_MODE)"

for i in $(seq 1 "$PAGE_COUNT"); do
  num=$(printf "%02d" "$i")
  png="$WORKDIR/page_${num}.png"
  out="$OUT_DIR/page_${num}.svg"

  echo "  page $i -> $out"
  if ! inkscape "$PDF" \
    --pages="$i" \
    --export-type=png \
    "--export-dpi=$EXPORT_DPI" \
    "--export-filename=$png" \
    2>/dev/null
  then
    echo "ERROR: inkscape failed on page $i" >&2
    exit 1
  fi

  if [[ ! -f "$png" ]]; then
    echo "ERROR: failed to write $png" >&2
    exit 1
  fi

  if [[ "$TRACE_MODE" == "potrace" ]]; then
    pgm="$WORKDIR/page_${num}.pgm"
    pbm="$WORKDIR/page_${num}.pbm"
    raw_svg="$WORKDIR/page_${num}_raw.svg"
    $MAGICK "$png" "$pgm"
    # shellcheck disable=SC2086
    mkbitmap $MKBITMAP_OPTS "$pgm" -o "$pbm"
    # shellcheck disable=SC2086
    potrace $POTRACE_OPTS -s -o "$raw_svg" "$pbm"
    "$PYTHON" "$NORMALIZE" "$raw_svg" "$out"
  else
    # min-polyline-points=4: require ≥3 segments (~0.25mm at 300dpi) to keep a skeleton chain.
    # Removes 1-2px junction artifacts before SVG. Override: SKELETON_MIN_POINTS=N
    SKELETON_MIN_PTS="${SKELETON_MIN_POINTS:-4}"
    "$PYTHON" "$SKELETON" "$png" "$out" --min-polyline-points "$SKELETON_MIN_PTS"
  fi
done

echo "✅ extracted $(ls -1 "$OUT_DIR"/page_*.svg 2>/dev/null | wc -l | tr -d ' ') SVG files ($TRACE_MODE)"
