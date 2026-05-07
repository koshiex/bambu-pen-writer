#!/bin/bash
# Extract every page of a PDF to separate SVG.
# Args: $1 = path to PDF (default: pdfs/2.pdf)
#       $2 = output dir for SVGs (default: build/svg)

set -euo pipefail

PDF="${1:-pdfs/2.pdf}"
OUT_DIR="${2:-build/svg}"

if [[ ! -f "$PDF" ]]; then
  echo "ERROR: $PDF not found" >&2
  exit 1
fi

if ! command -v inkscape >/dev/null 2>&1; then
  echo "ERROR: inkscape not in PATH" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# Detect page count: mdls (macOS native) -> Python regex fallback
PAGE_COUNT=""
if command -v mdls >/dev/null 2>&1; then
  PAGE_COUNT=$(mdls -name kMDItemNumberOfPages -raw "$PDF" 2>/dev/null || true)
fi

if ! [[ "$PAGE_COUNT" =~ ^[0-9]+$ ]] || [[ "$PAGE_COUNT" -lt 1 ]]; then
  PAGE_COUNT=$(python3 -c "
import re, sys
data = open('$PDF', 'rb').read()
print(len(re.findall(rb'/Type\s*/Page[^s]', data)))
" 2>/dev/null || echo "")
fi

if ! [[ "$PAGE_COUNT" =~ ^[0-9]+$ ]] || [[ "$PAGE_COUNT" -lt 1 ]]; then
  echo "ERROR: cannot detect page count of $PDF" >&2
  exit 1
fi

echo "Extracting $PAGE_COUNT pages from $PDF -> $OUT_DIR"

for i in $(seq 1 "$PAGE_COUNT"); do
  num=$(printf "%02d" "$i")
  out="$OUT_DIR/page_${num}.svg"
  echo "  page $i -> $out"
  inkscape "$PDF" \
    --pages="$i" \
    --export-text-to-path \
    --export-filename="$out" \
    2>&1 | grep -v "Empty path" || true
done

echo "✅ extracted $(ls -1 "$OUT_DIR"/page_*.svg | wc -l | tr -d ' ') SVG files"
