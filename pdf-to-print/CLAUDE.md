# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

PDF → G-code pipeline that turns a Bambu Lab P1S 3D printer into a pen plotter. Input is a 24-page handwriting-font PDF (`pdfs/2.pdf`, 165×205 mm portrait); output is a single self-contained `output/<pdf>[_experimental].gcode` (~19 MB) that draws the pages into a physical school notebook via a spring-loaded pen holder (soft holder is the main one; UMTS supported). Designed for one specific hardware setup, not generic plotting.

Authoritative docs: `docs/pipeline.md` (tech), `docs/operator-manual.md` (hardware/workflow), `docs/spread-print-order.md` (24-page saddle-stitch order), `docs/umts-p1s-pen.md` (holder Z-offset background), `docs/runlog.md` (calibration history). Read these first when picking up the project.

## Build / run

```bash
./scripts/build.sh                                         # pdfs/2.pdf → output/notebook.gcode (raster, sequential)
./scripts/build.sh pdfs/2.pdf output/notebook.gcode raster spread   # 24-page unfolded signature order
./scripts/build_experimental.sh                            # variable Z pressure + F jitter + strikethrough + spread + soft holder
```

`build.sh` runs 5 phases: (0) `e2e_reading_order_pipeline.py` synthetic smoke, (1) PDF→SVG via `extract_pages_raster.sh` (default) or `extract_pages.sh` (`EXTRACT=vector`), (2) `svg_to_gcode.py` per page, (2b) `validate_reading_order_gcode.py --strict` per page, (3) `merge_pages.py` concatenation with Bambu header/config blocks + page-pause templates (refuses pages whose pen-down Z belongs to another holder), (4) `printer_sim.py` dry run of the whole job + fidelity against the page rasters kept in `build/<pdf>/png` — the build fails on any error (`PDF_TO_PRINT_SIMULATE=0` skips).

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

`EXTRACT_MODE` (`raster`/`vector`), `TRACE_MODE` (`skeleton`/`potrace`), `EXPORT_DPI` (default 300), `SKELETON_MIN_POINTS`, `SKELETON_TOPOLOGY` (`euler` default / `legacy`), `PDF_TO_PRINT_Z_HOP_MM` (overrides holder hop), `PDF_TO_PRINT_DRAW_SPEED_MM_S` (default 100), `PDF_TO_PRINT_DRAW_ACCEL_MM_S2` (default 5000, emitted as `M204 S`), `PDF_TO_PRINT_TIME_FACTOR` (real/model time for `M73`), `PDF_TO_PRINT_PAGE_ORDER` (`sequential`/`spread`), `PDF_TO_PRINT_SKIP_LINEMERGE`, `PDF_TO_PRINT_SOFT_HOLDER`, `PDF_TO_PRINT_STROKE_MIN_LENGTH_MM`, `READING_FORCE_AXIS` (default `x`), `PDF_TO_PRINT_READING_INVERT_Y`, `PDF_TO_PRINT_READING_ROW_GAP_MM`, `PDF_TO_PRINT_READING_AXIS_AUTO`, `PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_PRESSURE`/`_FEEDRATE`/`_STRIKETHROUGH`, `PDF_TO_PRINT_PAUSE_BEEP` (default `1` — M1006 chirp before each flip-pause and end-pause; set `0` to silence).

## Tests

No pytest/CI (pytest is not in the venv). Bare-`assert` scripts, all `python3 scripts/<name>.py`:

- `scripts/test_skeleton_graph.py` — skeleton → Euler trails (junctions, spurs, retrace, smoothing, CLI).
- `scripts/test_plot_time_sim.py` — planner time model against analytic trapezoids.
- `scripts/test_motion_config.py` — hop / draw speed / accel env overrides and range checks.
- `scripts/test_merge_pages.py` — `M73` from simulated page times.
- `scripts/test_sheet_generators.py` — calibration sheets (zbench, hop-ladder, speed, page, edge-check).
- `scripts/test_compare_ink.py` — rendered-ink diff tool.
- `scripts/test_webui_server.py`, `scripts/test_webui_printer.py` — web UI (security, jobs, LAN printer flow against `webui/fake_printer.py`).
- `scripts/test_l_stop_model.py` — printable L-stop jig STL (watertight, fence faces at `PAPER_LEFT`/`PAPER_FRONT`).
- `scripts/test_printer_sim.py`, `scripts/test_printer_fidelity.py` — whole-job simulator (drag, off-paper ink, pauses, homing, stops, envelope) and fidelity vs source raster (runs vpype).
- `scripts/test_gcode_experimental.py` — bare-`assert` smoke for `gcode_experimental` (variance + strikethrough geometry).
- `scripts/e2e_reading_order_pipeline.py` — synthetic SVG → svg_to_gcode → validator inside a tempdir. Also runs as Phase 0 of `build.sh`.
- `scripts/page_order.py` — self-test under `__main__`.

`validate_reading_order_gcode.py --strict` can be run on any `build/gcode/page_*.gcode` to check the row-major permutation matches the generator byte-for-byte.

**Web UI.** `./scripts/webui.sh` (`python -m webui`, stdlib only) wraps the whole pipeline and sends jobs to the P1S over LAN (FTPS + MQTT `project_file`). `--demo-printer` runs `webui/fake_printer.py` instead of hardware. Printer commands need LAN Only + Developer Mode on firmware ≥ 01.08.02; status and FTPS upload work without.

## Architecture — non-obvious bits

**Coordinate frames.** G-code targets the **nozzle**, but the **pen** sits at a holder-dependent offset: UMTS (−26.46, −37.9) mm; soft holder (−38.34, −21.13) mm and +33.5 mm Z. Paper origin is bed-relative (24 mm from left, 50 mm from front). All offset math lives in `scripts/holder_config.py` — edit `UMTS_Z_PEN_DOWN` (currently 40.7 mm) and rerun `build.sh` to recalibrate pen depth, though the operator manual prefers physical adjustment in the holder.

**Reading-order axis.** After vpype's `pagerotate CW` + `scale 1 -1` (portrait → landscape + Y flip), `gcode_x = 255.46 − y_svg` (descending X = top row first) and `gcode_y = 87.9 + x_svg` (ascending Y = left→right). Therefore `--reading-force-axis x` is correct. Legacy `y` axis shreds rows into vertical bands. Row clustering threshold: `READING_ROW_GAP_BREAK_MM = 2.5`. The strict validator (`validate_reading_order_gcode.py`) reparses the emitted G-code and re-runs the **identical** permutation logic — any rounding drift between generator and validator (`READING_SORT_KEY_DECIMALS=3`, `_AGG_DECIMALS=6`) breaks Phase 2b.

**Page time = pen lifts.** Z moves the whole bed (20 mm/s, 500 mm/s², jerk 3); each pen lift is two Z moves. On the legacy tracer ~75 % of a page was Z, and drawing averaged 26 mm/s regardless of the 500 mm/s feed (pixel-staircase corners under jerk 9). So speed work targets lift count (`skeleton_graph.py` Euler trails) and hop height (`PDF_TO_PRINT_Z_HOP_MM`), not feed. Measure with `scripts/plot_time_sim.py`; check geometry changes with `scripts/compare_ink.py`; every build is dry-run by `scripts/printer_sim.py` (Phase 4). Calibration sheets for hop / speed / edge clearance: `scripts/calibration_sheets_gcode.py`. Printable L-stop jig: `scripts/l_stop_model.py` → `output/l_stop.stl`.

**Skeleton tracer.** Default `skeleton_graph.trace_strokes` (topology `euler`): drops diagonal pixel links that duplicate a 4-connected detour (otherwise every staircase corner looks like a junction), clusters junction pixels, prunes end spurs < 0.35 mm, draws each connected component as Euler trails (retrace ≤ 1 mm instead of a lift, straightest continuation at junctions), then Gaussian smoothing + RDP. `SKELETON_TOPOLOGY=legacy` restores the old one-chain-per-junction walk. vpype `filter --min-length 0.3mm` still runs after.

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
- `build/<pdf>/gcode/` — `page_NN.gcode` per page, no headers (~0.6–1 MB each with Euler trails, gitignored).
- `output/` — final `<pdf>[_experimental].gcode` (~19 MB) plus `calibration.gcode`, alignment template and `test_*.gcode` calibration sheets from `scripts/calibration_sheets_gcode.py` (gitignored).
- `fonts/`, `shiki(6).ttf` — handwriting fonts used to generate the input PDFs externally.
