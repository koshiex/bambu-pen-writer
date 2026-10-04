#!/usr/bin/env python3
"""Smoke tests for plot_time_sim (P1S planner time estimate).

Run: python3 scripts/test_plot_time_sim.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_time_sim import estimate_lines  # noqa: E402


def approx(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def test_z_move_matches_trapezoid_with_p1s_limits() -> None:
    # 6 mm at F1800 is capped to Z 20 mm/s; accel 500, jerk 3 → 0.329 s
    t = estimate_lines(["G1 Z10 F1800", "G1 Z16 F1800"], start=(0.0, 0.0, 10.0))
    assert approx(t.z, 0.329, 0.005), t
    assert t.draw == 0 and t.travel == 0


def test_long_straight_draw_move() -> None:
    # 100 mm at 100 mm/s with M204 S5000, XY jerk 9 → 1.0166 s
    t = estimate_lines(["M204 S5000", "G1 X100 Y0 F6000"])
    assert approx(t.draw, 1.0166, 0.002), t


def test_collinear_split_costs_nothing() -> None:
    one = estimate_lines(["M204 S5000", "G1 X100 Y0 F6000"])
    two = estimate_lines(["M204 S5000", "G1 X50 Y0 F6000", "G1 X100 Y0 F6000"])
    assert approx(one.total, two.total, 1e-6), (one, two)


def test_sharp_corner_costs_time() -> None:
    straight = estimate_lines(["M204 S5000", "G1 X20 Y0 F6000", "G1 X40 Y0 F6000"])
    corner = estimate_lines(["M204 S5000", "G1 X20 Y0 F6000", "G1 X20 Y20 F6000"])
    assert corner.total > straight.total + 0.015, (straight, corner)


def test_m204_changes_acceleration() -> None:
    slow = estimate_lines(["M204 S3000", "G1 X10 Y0 F30000"])
    fast = estimate_lines(["M204 S20000", "G1 X10 Y0 F30000"])
    assert fast.total < slow.total, (slow, fast)


def test_pen_lifts_and_kinds_are_counted() -> None:
    lines = [
        "G0 X10 Y10 F30000", "G1 Z74.2 F1800", "G1 X20 Y10 F6000", "G1 Z77.2 F1800",
        "G0 X30 Y10 F30000", "G1 Z74.2 F1800", "G1 X40 Y10 F6000", "G1 Z77.2 F1800",
    ]
    t = estimate_lines(lines, start=(0.0, 0.0, 77.2))
    assert t.pen_downs == 2, t
    assert t.travel > 0 and t.draw > 0 and t.z > 0


def test_page_file_starts_pen_up_at_first_stroke() -> None:
    """A page file starts after a pause (pen up); no phantom move from the origin."""
    import tempfile

    from plot_time_sim import estimate_file

    lines = ["G0 X100 Y100 F30000", "G1 Z74.2 F1800", "G1 X110 Y100 F6000", "G1 Z80.2 F1800",
             "G0 X120 Y100 F30000", "G1 Z74.2 F1800", "G1 X130 Y100 F6000", "G1 Z80.2 F1800"]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "page_01.gcode"
        p.write_text("\n".join(lines) + "\n")
        t = estimate_file(p, 5000)
    assert t.pen_downs == 2, t
    assert approx(t.z, 4 * 0.329, 0.01), t          # two lifts × (down + up) of 6 mm
    assert approx(t.travel_mm, 10.0, 1e-6), t       # only the hop between the strokes


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_plot_time_sim: {len(tests)} passed")


if __name__ == "__main__":
    main()
