#!/usr/bin/env python3
"""Convert SVG pages from build/svg/ into per-page G-code drawing blocks.

Pipeline per SVG:
  1. read SVG (--quantization READ_QUANTIZATION; --single-layer; no --simplify)
  2. pagerotate (CCW 90°, portrait -> landscape)
  3. scale 1 -1 around origin (flip Y, SVG-down -> Bambu-up)
  4. translate to paper origin in nozzle frame (PAPER_ORIGIN_*)
  5. optional linemerge (--skip-linemerge to disable)
  6. merge all vpype layers into one, then sort strokes for reading order (gap-clustered rows; default axis y)
  7. gwrite with `bambu_p1s_umts` profile (no header/footer, just G0/G1 + Z-hop)

Output: build/gcode/page_NN.gcode

Usage:
  python3 scripts/svg_to_gcode.py [--svg-dir DIR] [--out-dir DIR]

Requires:
  - Inkscape and vpype installed
  - .vpype.toml in HOME with [gwrite.bambu_p1s_umts] section (see templates/vpype_profile.toml)
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

import numpy as np
import vpype as vp
from vpype_cli import execute

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from gcode_stroke_parse import parse_stroke_polylines  # noqa: E402

# Pen offset relative to nozzle (firmware coords). Pen is shifted toward bed
# origin (0, 0) from nozzle position. Measured with caliper:
#   delta_X = 26.46 mm  (pen 26.46 mm toward bed-left from nozzle)
#   delta_Y = 37.9  mm  (pen 37.9 mm toward bed-front from nozzle)
# Diagonal sanity: √(26.46² + 37.9²) = 46.2 mm ≈ measured 46.5 ✓
PEN_OFFSET_X = -26.46
PEN_OFFSET_Y = -37.9

# Paper position in PEN frame (where ink lands on bed). User-friendly:
# bed-relative — measure with ruler from bed origin (front-left corner of plate).
# Constraint: paper_X_max + |PEN_OFFSET_X| ≤ 256 → paper_X_min ≤ 24.5
#             paper_Y_max + |PEN_OFFSET_Y| ≤ 256 → paper_Y_min ≤ 53.1
PAPER_LEFT  = 24.0     # mm from bed left edge to paper left
PAPER_FRONT = 50.0     # mm from bed front edge to paper front
PAPER_W     = 205.0    # tetradka landscape width (along X)
PAPER_H     = 165.0    # tetradka landscape height (along Y)

# Paper bounds (pen frame): X∈[24, 229], Y∈[50, 215]
# Nozzle bounds (gcode):    X∈[50.46, 255.46], Y∈[87.9, 252.9] ✓ within bed [0, 256]
#
# vpype `translate` target = NOZZLE frame (firmware coords). After scale 1 -1,
# content Y becomes negative; translate by paper rear edge in nozzle frame
# (paper_rear_pen + |PEN_OFFSET_Y| = 215 + 37.9 = 252.9).
PAPER_ORIGIN_X = f"{PAPER_LEFT - PEN_OFFSET_X}mm"            # 50.46
PAPER_ORIGIN_Y = f"{PAPER_FRONT + PAPER_H - PEN_OFFSET_Y}mm"  # 252.9

# Z-offset (mm) — distance pen tip extends BELOW nozzle when UMTS module loaded.
# Critical: raw G-code we generate uses absolute Z values in NOZZLE frame.
# When pen touches paper, nozzle is at Z = Z_PEN_DOWN above bed.
# When pen lifts (Z-hop), nozzle is at Z = Z_PEN_DOWN + Z_HOP.
# Source: UMTS docs §Z-Offset: +17 Stabilo, +20 POSCA. Xiaomi gel ≈ +18 (between).
# Calibrate live (Test 3 of operator-manual.md) and update here.
Z_PEN_DOWN = 18.0
Z_HOP = 15.0

VPYPE_PROFILE = "bambu_p1s_umts"
# Curve linearization on SVG import (vpype read --quantization). Finer = more vertices / larger G-code.
READ_QUANTIZATION = "0.05mm"
LINEMERGE_TOLERANCE = "0.05mm"

# Stroke order: row-major — see apply_reading_order_sort().
# True = smaller machine Y first after transforms (typical “top of notebook first” on P1S + UMTS).
# If your bed/paper frame needs the opposite, set READING_SORT_INVERT_Y=False or PDF_TO_PRINT_READING_INVERT_Y=0.
READING_SORT_INVERT_Y = True
# New row when consecutive stroke centroids differ by more than this along the row axis (mm).
# Clustering uses centroids in gwrite-equivalent mm (see reading_stroke_metrics).
READING_ROW_GAP_BREAK_MM = 4.5
# Legacy heuristic (only when PDF_TO_PRINT_READING_AXIS_AUTO=1 and axis is "auto"):
# if range_y < range_x * ratio, treat strokes as vertical bands (column-major).
READING_ROW_AXIS_RATIO = 0.45
# Default: row-major with horizontal lines on the bed (sort by Y bands, then X). Prevents
# wide pages from switching to "one sweep left→right". Enable old auto heuristic via env.
READING_ROW_AXIS_AUTO = False

# XY feed for pen-down moves (G1 … X Y F…). Marlin/Bambu use mm/min → mm/s × 60.
DRAW_SPEED_MM_S = 50.0
DRAW_FEED_MM_MIN = int(DRAW_SPEED_MM_S * 60)

# Must match gwrite `{{x:.3f}}` / `{{y:.3f}}` in write_vpype_profile() so sort keys match
# `validate_reading_order_gcode` (parsed G-code uses the same precision).
READING_SORT_KEY_DECIMALS = 3


def _mm_per_internal_unit() -> float:
    """vpype document coords before gwrite scale; gwrite does scale(1/convert_length('1mm'), …)."""
    return float(vp.convert_length("1mm"))


def reading_vertex_xy_mm_rounded(
    a: np.ndarray,
    *,
    coords_are_mm: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """Vertex arrays in mm with same rounding as emitted G-code."""
    if coords_are_mm:
        xr = np.round(np.asarray(a.real, dtype=float), READING_SORT_KEY_DECIMALS)
        yi = np.round(np.asarray(a.imag, dtype=float), READING_SORT_KEY_DECIMALS)
    else:
        s = _mm_per_internal_unit()
        xr = np.round(np.asarray(a.real, dtype=float) / s, READING_SORT_KEY_DECIMALS)
        yi = np.round(np.asarray(a.imag, dtype=float) / s, READING_SORT_KEY_DECIMALS)
    return xr, yi


def reading_stroke_metrics(
    arrays: list[np.ndarray],
    *,
    coords_are_mm: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Centroids and min/mean X in gwrite-equivalent mm (matches validator on page G-code)."""
    min_x: list[float] = []
    mean_x: list[float] = []
    mean_y: list[float] = []
    cx: list[float] = []
    cy: list[float] = []
    for a in arrays:
        xr, yi = reading_vertex_xy_mm_rounded(a, coords_are_mm=coords_are_mm)
        min_x.append(float(np.min(xr)))
        mean_x.append(float(np.mean(xr)))
        my = float(np.mean(yi))
        mean_y.append(my)
        cx.append(float(np.mean(xr)))
        cy.append(my)
    return (
        np.asarray(cx),
        np.asarray(cy),
        np.asarray(min_x),
        np.asarray(mean_x),
        np.asarray(mean_y),
    )


def write_vpype_profile() -> None:
    """Write ~/.vpype.toml with Z constants substituted. Required because vpype
    gwrite reads profiles from ~/.vpype.toml — we cannot pass Z inline."""
    z_down = Z_PEN_DOWN
    z_up = Z_PEN_DOWN + Z_HOP
    content = f"""# Auto-generated by scripts/svg_to_gcode.py.
# Source of truth: Z_PEN_DOWN={Z_PEN_DOWN}, Z_HOP={Z_HOP} in svg_to_gcode.py.
# To change pen Z calibration: edit svg_to_gcode.py and re-run.

[gwrite.bambu_p1s_umts]
unit = "mm"
vertical_flip = false
document_start = ""
document_end = ""
layer_start = ""
layer_end = ""
line_start = ""
segment_first = "G0 X{{x:.3f}} Y{{y:.3f}} F18000\\nG1 Z{z_down:.3f} F1200\\n"
segment = "G1 X{{x:.3f}} Y{{y:.3f}} F{DRAW_FEED_MM_MIN}\\n"
line_end = "G1 Z{z_up:.3f} F1200\\n"
info = "Bambu Lab P1S + UMTS pen plotter. Z_PEN_DOWN={Z_PEN_DOWN} Z_HOP={Z_HOP} draw={DRAW_SPEED_MM_S}mm/s"
"""
    target = Path.home() / ".vpype.toml"
    target.write_text(content)
    print(f"  wrote {target} (Z down={z_down}, Z up={z_up})")


def _effective_reading_sort_invert_y(cli_invert: bool) -> bool:
    env = os.environ.get("PDF_TO_PRINT_READING_INVERT_Y", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    if cli_invert:
        return True
    return READING_SORT_INVERT_Y


def resolve_reading_invert_y(*, cli_invert: bool, cli_no_invert: bool) -> bool:
    """--no-invert-reading-sort overrides env; --invert-reading-sort forces True; else env + default."""
    if cli_no_invert:
        return False
    if cli_invert:
        return True
    return _effective_reading_sort_invert_y(False)


def _effective_reading_row_gap_mm(cli_gap: float | None) -> float:
    if cli_gap is not None and cli_gap > 0:
        return cli_gap
    for key in ("PDF_TO_PRINT_READING_ROW_GAP_MM", "PDF_TO_PRINT_READING_ROW_BUCKET_MM"):
        env = os.environ.get(key, "").strip()
        if env:
            try:
                v = float(env)
                if v > 0:
                    return v
            except ValueError:
                pass
    return READING_ROW_GAP_BREAK_MM


def _effective_reading_force_axis() -> str | None:
    """Return 'y', 'x', or None for auto."""
    v = os.environ.get("PDF_TO_PRINT_READING_FORCE_AXIS", "").strip().lower()
    if v in ("y", "x"):
        return v
    return None


def _effective_reading_axis_auto() -> bool:
    """When True, `auto` axis uses READING_ROW_AXIS_RATIO vs layout extents."""
    v = os.environ.get("PDF_TO_PRINT_READING_AXIS_AUTO", "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return READING_ROW_AXIS_AUTO


def reading_rows_along_y_decision(
    cx: np.ndarray,
    cy: np.ndarray,
    *,
    force_axis: str | None,
) -> bool:
    """True = rows are horizontal on the bed (cluster by Y, order within row by X)."""
    axis = force_axis if force_axis in ("y", "x") else _effective_reading_force_axis()
    range_x = float(np.ptp(cx))
    range_y = float(np.ptp(cy))
    if axis == "y":
        return True
    if axis == "x":
        return False
    if _effective_reading_axis_auto():
        return range_y >= range_x * READING_ROW_AXIS_RATIO
    return True


def _assign_row_ids_gap(
    primary: np.ndarray, sorted_idx: np.ndarray, gap: float, *, ascending: bool
) -> np.ndarray:
    """Sorted order: if ascending, sorted_idx sorts primary low→high; else high→low."""
    n = len(primary)
    row_id = np.zeros(n, dtype=int)
    rid = 0
    row_id[sorted_idx[0]] = 0
    for k in range(1, n):
        a = primary[sorted_idx[k - 1]]
        b = primary[sorted_idx[k]]
        delta = (b - a) if ascending else (a - b)
        if delta > gap:
            rid += 1
        row_id[sorted_idx[k]] = rid
    return row_id


def reading_row_metadata_from_lines(
    arrays: list[np.ndarray],
    *,
    invert_y: bool,
    row_gap_mm: float,
    force_axis: str | None = None,
    coords_are_mm: bool = False,
) -> tuple[bool, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return rows_along_y, row_id, and min/mean X and mean Y per stroke in gwrite-equivalent mm.

    coords_are_mm: True when arrays come from G-code (mm); False for vpype Document lines (px).
    """
    n = len(arrays)
    row_id = np.zeros(max(n, 1), dtype=int)
    if n == 0:
        z = np.zeros(0)
        return True, row_id[:0], z, z, z
    if n == 1:
        _, _, min_x, mean_x, mean_y_c = reading_stroke_metrics(
            arrays, coords_are_mm=coords_are_mm
        )
        return True, row_id[:1], min_x, mean_x, mean_y_c

    cx, cy, min_x, mean_x, mean_y_c = reading_stroke_metrics(
        arrays, coords_are_mm=coords_are_mm
    )
    rows_along_y = reading_rows_along_y_decision(cx, cy, force_axis=force_axis)
    # Centroids are in mm (gwrite-equivalent); gap is mm.
    gap = float(row_gap_mm)

    if rows_along_y:
        if invert_y:
            sorted_idx = np.argsort(cy)
            ascending = True
        else:
            sorted_idx = np.argsort(-cy)
            ascending = False
        row_id = _assign_row_ids_gap(cy, sorted_idx, gap, ascending=ascending)
    else:
        sorted_idx = np.argsort(cx)
        row_id = _assign_row_ids_gap(cx, sorted_idx, gap, ascending=True)

    return rows_along_y, row_id, min_x, mean_x, mean_y_c


def reading_order_permutation_from_lines(
    arrays: list[np.ndarray],
    *,
    invert_y: bool,
    row_gap_mm: float,
    force_axis: str | None = None,
    coords_are_mm: bool = False,
) -> np.ndarray:
    """Return indices such that arrays[i] in returned order is reading order (row-major)."""
    n = len(arrays)
    if n < 2:
        return np.arange(n)

    rows_along_y, row_id, min_x, mean_x, mean_y_c = reading_row_metadata_from_lines(
        arrays,
        invert_y=invert_y,
        row_gap_mm=row_gap_mm,
        force_axis=force_axis,
        coords_are_mm=coords_are_mm,
    )

    if rows_along_y:
        decorated = [
            (int(row_id[i]), min_x[i], mean_x[i], mean_y_c[i], i)
            for i in range(n)
        ]
    else:
        decorated = [
            (int(row_id[i]), -mean_y_c[i], mean_x[i], min_x[i], i)
            for i in range(n)
        ]
    decorated.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))
    return np.array([t[-1] for t in decorated], dtype=int)


def merge_all_layers_to_layer_one(doc: vp.Document) -> None:
    """Flatten all vpype layers into layer 1 (same stroke order as gwrite multi-layer output).

    Reading-order sort must see one sequence of lines; otherwise we sort each layer in isolation
    while gwrite concatenates layers — validation and row-major order both break.
    """
    ld = doc.layers
    if len(ld) <= 1:
        return
    combined: list[np.ndarray] = []
    meta: dict | None = None
    for _lid, lc in ld.items():
        if meta is None:
            meta = lc.metadata
        combined.extend(lc.lines)
    ld.clear()
    ld[1] = vp.LineCollection(lines=combined, metadata=meta or {})


def apply_reading_order_sort(
    doc: vp.Document,
    *,
    invert_y: bool,
    row_gap_mm: float,
    force_axis: str | None = None,
) -> None:
    """Sort strokes row-major using gap clustering on centroid coordinates."""
    for lid, lc in list(doc.layers.items()):
        lines = lc.lines
        if len(lines) < 2:
            continue
        arrays = [np.asarray(ln, dtype=np.complex128) for ln in lines]
        perm = reading_order_permutation_from_lines(
            arrays,
            invert_y=invert_y,
            row_gap_mm=row_gap_mm,
            force_axis=force_axis,
        )
        ordered = [arrays[i] for i in perm]
        doc.layers[lid] = vp.LineCollection(lines=ordered, metadata=lc.metadata)


def _document_line_count(doc: vp.Document) -> int:
    return sum(len(lc.lines) for lc in doc.layers.values())


def assert_document_is_reading_sorted(
    doc: vp.Document,
    *,
    invert_y: bool,
    row_gap_mm: float,
    force_axis: str | None,
) -> None:
    """Raise RuntimeError if LineCollection order is not canonical reading order."""
    for lid, lc in doc.layers.items():
        lines = lc.lines
        if len(lines) < 2:
            continue
        arrays = [np.asarray(ln, dtype=np.complex128) for ln in lines]
        perm = reading_order_permutation_from_lines(
            arrays,
            invert_y=invert_y,
            row_gap_mm=row_gap_mm,
            force_axis=force_axis,
        )
        if not np.all(perm == np.arange(len(arrays))):
            raise RuntimeError(
                f"reading-order invariant failed after sort (layer {lid}): "
                f"perm[:24]={perm[:24].tolist()}"
            )


def convert_one(
    svg: Path,
    out: Path,
    *,
    skip_linemerge: bool = False,
    invert_reading_sort: bool = False,
    no_invert_reading_sort: bool = False,
    reading_row_gap_mm: float | None = None,
    reading_force_axis: str | None = None,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    invert_y = resolve_reading_invert_y(
        cli_invert=invert_reading_sort,
        cli_no_invert=no_invert_reading_sort,
    )
    gap_mm = _effective_reading_row_gap_mm(reading_row_gap_mm)
    axis = reading_force_axis if reading_force_axis in ("y", "x") else None
    qs = shlex.quote(str(svg.resolve()))
    qout = shlex.quote(str(out.resolve()))
    parts = [
        "read",
        "--quantization",
        READ_QUANTIZATION,
        "--single-layer",
        qs,
        "pagerotate",
        "--clockwise",
        "scale",
        "-o",
        "0",
        "0",
        "--",
        "1",
        "-1",
        "translate",
        PAPER_ORIGIN_X,
        PAPER_ORIGIN_Y,
    ]
    if not skip_linemerge:
        parts.extend(["linemerge", "--tolerance", LINEMERGE_TOLERANCE])
    preprocess = " ".join(parts)
    doc = execute(preprocess)
    merge_all_layers_to_layer_one(doc)
    apply_reading_order_sort(doc, invert_y=invert_y, row_gap_mm=gap_mm, force_axis=axis)
    assert_document_is_reading_sorted(doc, invert_y=invert_y, row_gap_mm=gap_mm, force_axis=axis)
    execute(f"gwrite -p {VPYPE_PROFILE} {qout}", document=doc)

    n_vpype = _document_line_count(doc)
    strokes_parsed = parse_stroke_polylines(out, Z_PEN_DOWN, Z_PEN_DOWN + Z_HOP)
    if len(strokes_parsed) != n_vpype:
        raise RuntimeError(
            f"{out.name}: parsed G-code strokes ({len(strokes_parsed)}) != vpype lines ({n_vpype}) "
            "— check gcode_stroke_parse or stray non-vpype commands in the file"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--svg-dir", default="build/svg",
                        help="directory with page_NN.svg (default: build/svg)")
    parser.add_argument("--out-dir", default="build/gcode",
                        help="output directory (default: build/gcode)")
    parser.add_argument(
        "--skip-linemerge",
        action="store_true",
        help="omit linemerge (more pen-ups; reading-order sort still applies)",
    )
    inv = parser.add_mutually_exclusive_group()
    inv.add_argument(
        "--invert-reading-sort",
        action="store_true",
        help="force vertical reading order invert_y=True (overrides env)",
    )
    inv.add_argument(
        "--no-invert-reading-sort",
        action="store_true",
        help="force invert_y=False (bottom-row-first / legacy; overrides env and default)",
    )
    parser.add_argument(
        "--reading-row-gap-mm",
        type=float,
        default=None,
        metavar="MM",
        dest="reading_row_gap_mm",
        help="gap (mm) to split rows via centroid clustering (env PDF_TO_PRINT_READING_ROW_GAP_MM)",
    )
    parser.add_argument(
        "--reading-force-axis",
        choices=("auto", "y", "x"),
        default="y",
        help="stroke row axis: y=horizontal lines on bed (default), x=columns, auto=see PDF_TO_PRINT_READING_AXIS_AUTO",
    )
    args = parser.parse_args()

    svg_dir = Path(args.svg_dir)
    out_dir = Path(args.out_dir)

    svgs = sorted(svg_dir.glob("page_*.svg"))
    if not svgs:
        sys.exit(f"ERROR: no page_*.svg in {svg_dir}")

    write_vpype_profile()

    print(f"Converting {len(svgs)} SVGs -> {out_dir}")
    for svg in svgs:
        out = out_dir / (svg.stem + ".gcode")
        print(f"  {svg.name} -> {out.name}")
        fa = None if args.reading_force_axis == "auto" else args.reading_force_axis
        convert_one(
            svg,
            out,
            skip_linemerge=args.skip_linemerge,
            invert_reading_sort=args.invert_reading_sort,
            no_invert_reading_sort=args.no_invert_reading_sort,
            reading_row_gap_mm=args.reading_row_gap_mm,
            reading_force_axis=fa,
        )

    total_size = sum(p.stat().st_size for p in out_dir.glob("page_*.gcode"))
    print(f"✅ generated {len(svgs)} G-code files ({total_size/1e6:.1f} MB total)")


if __name__ == "__main__":
    main()
