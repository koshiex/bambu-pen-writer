#!/usr/bin/env python3
"""Validate pen-down stroke order in vpype-generated page G-code (reading-order).

Expects the profile from svg_to_gcode.write_vpype_profile(): each stroke begins with
`segment_first` = G0 XY + G1 Z down (no ink move yet), then G1 XY moves, then line_end G1 Z up.

We reconstruct each stroke as a polyline: first vertex = G0 target (same as vpype line start),
plus every G1 XY until pen-up — then permutation logic matches svg_to_gcode (mean/min over vertices).

Exit 0 if stroke order matches row-major reading order. Exit 1 on mismatch or parse errors.

With --strict: additionally verifies consecutive strokes are non-decreasing under the same lex key as
``reading_order_permutation_from_lines`` (stable tie-break by stroke index).

Usage:
  python3 scripts/validate_reading_order_gcode.py build/gcode/page_01.gcode
  python3 scripts/validate_reading_order_gcode.py --strict --quiet build/gcode/page_01.gcode
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

# Import permutation logic + constants from sibling module
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from gcode_stroke_parse import parse_stroke_polylines  # noqa: E402
from svg_to_gcode import (  # noqa: E402
    Z_HOP,
    Z_PEN_DOWN,
    _effective_reading_row_gap_mm,
    reading_order_permutation_from_lines,
    reading_row_metadata_from_lines,
    resolve_reading_invert_y,
)


def strict_geometry_checks(
    arrays: list[np.ndarray],
    *,
    invert_y: bool,
    row_gap_mm: float,
    force_axis: str | None,
) -> tuple[bool, str]:
    """Verify consecutive strokes are non-decreasing under the same lex key as sorting.

    Matches ``reading_order_permutation_from_lines`` (including stable tie-break by stroke index).
    """
    n = len(arrays)
    rows_along_y, row_id, min_x, mean_x, mean_y_c, min_y_c = reading_row_metadata_from_lines(
        arrays,
        invert_y=invert_y,
        row_gap_mm=row_gap_mm,
        force_axis=force_axis,
        coords_are_mm=True,
    )

    for k in range(1, n):
        if rows_along_y:
            prev_k = (
                int(row_id[k - 1]),
                float(min_x[k - 1]),
                float(mean_x[k - 1]),
                float(mean_y_c[k - 1]),
                k - 1,
            )
            curr_k = (
                int(row_id[k]),
                float(min_x[k]),
                float(mean_x[k]),
                float(mean_y_c[k]),
                k,
            )
        else:
            # Matches reading_order_permutation_from_lines rows_along_y=False branch:
            # (-min_y_c, -mean_y_c, i) — descending Y = high-Y (left) first.
            prev_k = (
                int(row_id[k - 1]),
                float(-min_y_c[k - 1]),
                float(-mean_y_c[k - 1]),
                k - 1,
            )
            curr_k = (
                int(row_id[k]),
                float(-min_y_c[k]),
                float(-mean_y_c[k]),
                k,
            )
        if prev_k > curr_k:
            return False, (
                f"strict: sort key regresses at {k - 1}->{k} "
                f"(prev={prev_k[:-1]}, curr={curr_k[:-1]})"
            )
    return True, ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gcode", type=Path, help="page_NN.gcode from svg_to_gcode")
    parser.add_argument("--z-pen-down", type=float, default=Z_PEN_DOWN)
    inv = parser.add_mutually_exclusive_group()
    inv.add_argument("--invert-reading-sort", action="store_true")
    inv.add_argument("--no-invert-reading-sort", action="store_true")
    parser.add_argument("--reading-row-gap-mm", type=float, default=None)
    parser.add_argument(
        "--reading-force-axis",
        choices=("auto", "y", "x"),
        default="x",
        help="must match svg_to_gcode run (default x — portrait→landscape layout)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require monotonic row/band sequence and within-row order",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    gap = _effective_reading_row_gap_mm(args.reading_row_gap_mm)
    fa = None if args.reading_force_axis == "auto" else args.reading_force_axis

    invert_y = resolve_reading_invert_y(
        cli_invert=args.invert_reading_sort,
        cli_no_invert=args.no_invert_reading_sort,
    )

    z_up = args.z_pen_down + Z_HOP
    arrays = parse_stroke_polylines(args.gcode, args.z_pen_down, z_up)
    n = len(arrays)
    if n < 2:
        print(f"OK ({n} strokes, nothing to order-check)")
        raise SystemExit(0)

    rows_along_y, _, _, _, _, _ = reading_row_metadata_from_lines(
        arrays,
        invert_y=invert_y,
        row_gap_mm=gap,
        force_axis=fa,
        coords_are_mm=True,
    )

    perm = reading_order_permutation_from_lines(
        arrays,
        invert_y=invert_y,
        row_gap_mm=gap,
        force_axis=fa,
        coords_are_mm=True,
    )

    expected = np.arange(n, dtype=int)
    ok_perm = np.array_equal(perm, expected)

    ok_strict = True
    strict_msg = ""
    if args.strict:
        ok_strict, strict_msg = strict_geometry_checks(
            arrays,
            invert_y=invert_y,
            row_gap_mm=gap,
            force_axis=fa,
        )

    if not args.quiet:
        axis_label = "y" if rows_along_y else "x"
        print(
            f"strokes={n}  gap_mm={gap}  axis={axis_label} "
            f"(rows_along_y={rows_along_y})  invert_y={invert_y}"
        )
        print(f"permutation (want 0..{n - 1}): {perm.tolist()}")

    if ok_perm and ok_strict:
        msg = "reading-order OK (matches row-major permutation"
        if args.strict:
            msg += ", strict geometry OK"
        msg += ")"
        print(msg)
        raise SystemExit(0)

    if not ok_perm:
        print(
            "FAIL: G-code stroke order does not match canonical reading permutation.",
            file=sys.stderr,
        )
        print("  first divergence at sort position k where perm[k] != k", file=sys.stderr)
        for k in range(n):
            if perm[k] != k:
                print(
                    f"  k={k}: perm[k]={perm[k]} (draw stroke index {perm[k]} at step {k})",
                    file=sys.stderr,
                )
                break
    if args.strict and not ok_strict:
        print(f"FAIL: {strict_msg}", file=sys.stderr)

    raise SystemExit(1)


if __name__ == "__main__":
    main()
