# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

PDF → G-code pipeline that turns a Bambu Lab P1S 3D printer into a pen plotter. Input is a 24-page handwriting-font PDF (`pdfs/2.pdf`, 165×205 mm portrait); output is a single self-contained `output/notebook.gcode` (~141 MB) that draws the pages into a physical school notebook via the UMTS spring-loaded pen holder. Designed for one specific hardware setup, not generic plotting.

Authoritative docs: `docs/pipeline.md` (tech), `docs/operator-manual.md` (hardware/workflow), `docs/spread-print-order.md` (24-page saddle-stitch order), `docs/umts-p1s-pen.md` (holder Z-offset background), `docs/runlog.md` (calibration history). Read these first when picking up the project.

## Build / run

```bash
./scripts/build.sh                                         # pdfs/2.pdf → output/notebook.gcode (raster, sequential)
./scripts/build.sh pdfs/2.pdf output/notebook.gcode raster spread   # 24-page unfolded signature order
./scripts/build_experimental.sh                            # variable Z pressure + F jitter + strikethrough + spread + soft holder
```

`build.sh` runs 4 phases: (0) `e2e_reading_order_pipeline.py` synthetic smoke, (1) PDF→SVG via `extract_pages_raster.sh` (default) or `extract_pages.sh` (`EXTRACT=vector`), (2) `svg_to_gcode.py` per page, (2b) `validate_reading_order_gcode.py --strict` per page, (3) `merge_pages.py` concatenation with Bambu header/config blocks + page-pause templates.

Auto-activates `.venv/bin/activate` if present. No `requirements.txt` — env is hand-built. Python **3.13** (vpype is incompatible with 3.14 — `pkg_resources` removed). Key deps: `vpype 1.15`, `vpype-gcode`, `scikit-image`, `networkx`, `scipy`, `Pillow`, `shapely`. System tools: Inkscape ≥ 1.4 (needs `--pages=N`), `magick`, optional `potrace`+`mkbitmap`.

Direct sub-steps (useful for iteration):
```bash
./scripts/extract_pages_raster.sh pdfs/2.pdf build/svg
python3 scripts/svg_to_gcode.py --svg-dir build/svg --out-dir build/gcode --reading-force-axis x
python3 scripts/merge_pages.py --gcode-dir build/gcode --templates-dir templates --out output/notebook.gcode --page-order spread
python3 scripts/calibration_gcode.py     # output/calibration.gcode for pen-depth tuning
python3 scripts/alignment_gcode.py       # paper-placement template
```

### Env flags

`EXTRACT_MODE` (`raster`/`vector`), `TRACE_MODE` (`skeleton`/`potrace`), `EXPORT_DPI` (default 300), `SKELETON_MIN_POINTS`, `PDF_TO_PRINT_PAGE_ORDER` (`sequential`/`spread`), `PDF_TO_PRINT_SKIP_LINEMERGE`, `PDF_TO_PRINT_SOFT_HOLDER`, `PDF_TO_PRINT_STROKE_MIN_LENGTH_MM`, `READING_FORCE_AXIS` (default `x`), `PDF_TO_PRINT_READING_INVERT_Y`, `PDF_TO_PRINT_READING_ROW_GAP_MM`, `PDF_TO_PRINT_READING_AXIS_AUTO`, `PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_PRESSURE`/`_FEEDRATE`/`_STRIKETHROUGH`.

## Tests

No pytest/CI. Three manual smoke scripts, all `python3 scripts/<name>.py`:

- `scripts/test_gcode_experimental.py` — bare-`assert` smoke for `gcode_experimental` (variance + strikethrough geometry).
- `scripts/e2e_reading_order_pipeline.py` — synthetic SVG → svg_to_gcode → validator inside a tempdir. Also runs as Phase 0 of `build.sh`.
- `scripts/page_order.py` — self-test under `__main__`.

`validate_reading_order_gcode.py --strict` can be run on any `build/gcode/page_*.gcode` to check the row-major permutation matches the generator byte-for-byte.

## Architecture — non-obvious bits

**Coordinate frames.** G-code targets the **nozzle**, but the **pen** sits at a holder-dependent offset: UMTS (−26.46, −37.9) mm; soft holder (−38.34, −21.13) mm and +33.5 mm Z. Paper origin is bed-relative (24 mm from left, 50 mm from front). All offset math lives in `scripts/holder_config.py` — edit `UMTS_Z_PEN_DOWN` (currently 40.7 mm) and rerun `build.sh` to recalibrate pen depth, though the operator manual prefers physical adjustment in the holder.

**Reading-order axis.** After vpype's `pagerotate CW` + `scale 1 -1` (portrait → landscape + Y flip), `gcode_x = 255.46 − y_svg` (descending X = top row first) and `gcode_y = 87.9 + x_svg` (ascending Y = left→right). Therefore `--reading-force-axis x` is correct. Legacy `y` axis shreds rows into vertical bands. Row clustering threshold: `READING_ROW_GAP_BREAK_MM = 2.5`. The strict validator (`validate_reading_order_gcode.py`) reparses the emitted G-code and re-runs the **identical** permutation logic — any rounding drift between generator and validator (`READING_SORT_KEY_DECIMALS=3`, `_AGG_DECIMALS=6`) breaks Phase 2b.

**Skeleton tracer artifacts.** Raster→skeleton via `skimage.morphology.skeletonize` + networkx walk produces 1–3 px junction spurs (0.085–0.25 mm @ 300 DPI). Killed by vpype `filter --min-length 0.3mm`; dots on й/ё (≥ 0.42 mm) survive. Tunable via `PDF_TO_PRINT_STROKE_MIN_LENGTH_MM` and `SKELETON_MIN_POINTS`.

**Stroke orientation.** `_orient_strokes_left_to_right` in `svg_to_gcode.py` flips each open polyline so the higher-`gcode_y` (left) end is drawn first. Without it the skeleton walker emits strokes in arbitrary direction.

**Experimental post-process breaks the validator.** Strikethroughs and word jitter add extra strokes; `build.sh` skips Phase 2b when any `PDF_TO_PRINT_EXPERIMENTAL_*` flag is set. Sentinel `; === pdf-to-print experimental strokes ===` from `gcode_stroke_parse.EXPERIMENTAL_STROKES_MARKER` delimits the appended block.

**Page order `spread` requires N=24** (saddle-stitch signature). Per spread `s∈1..6`: `[2s−1, 2s, 25−2s, 26−2s]`. Hard error otherwise. See `docs/spread-print-order.md`.

## G-code output contract — do not casually touch

`output/notebook.gcode` is firmware-validated by P1S. Several non-negotiable invariants:

- **Bambu HEADER + CONFIG blocks in `merge_pages.py`** are copied verbatim from a real benchy slice. `patch_header` rewrites only `total layer number` and `model printing time`. Don't reformat or strip.
- **Send only via Orca Slicer → Device → Open G-code File** (LAN stream, byte-for-byte). Never drag-and-drop / "Slice" — Orca re-parses and breaks it. SD card may be rejected.
- **Pauses use `M400 U1` only.** P1S firmware ignores `M0`/`M601`/`M226`. Expected `grep -c 'M400 U1' output/notebook.gcode` ≈ 25 (1 install + 23 flip + 1 final).
- **`templates/bambu_start.gcode` is deliberately stripped** of `G29` ABL, `M970.3`/`M974` vibration check, `M976` heatbed scan, and the wipe sequence — these freeze the P1S when no filament is loaded. Do not re-add.
- **`templates/page_pause.gcode` placeholders** (`{NEXT_PAGE}`, `{PROGRESS}`, `{REMAINING}`, `{Z_TRAVEL_CLEARANCE}`, `{Z_TRAVEL_FEED}`, `{PARK_X}`, `{PARK_Y}`) are substituted by `merge_pages.patch_holder_templates`. Adding placeholders requires updating both files.
- **`~/.vpype.toml` and `./.vpype.generated.toml` are rewritten on every `svg_to_gcode.py` run** with current holder Z values baked in. `templates/vpype_profile.toml` is reference-only — editing it by hand has no effect.

## Inputs / outputs

- `pdfs/` — source PDFs (gitignored; current: `2.pdf`).
- `build/svg/` — `page_NN.svg` (24 files, gitignored).
- `build/gcode/` — `page_NN.gcode` per page, no headers (~5–7 MB each, gitignored).
- `output/` — final `notebook.gcode` (~141 MB) plus `calibration.gcode` / alignment template (gitignored).
- `fonts/`, `shiki(6).ttf` — handwriting fonts used to generate the input PDFs externally.
