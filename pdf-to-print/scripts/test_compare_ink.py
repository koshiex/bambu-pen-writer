#!/usr/bin/env python3
"""Smoke tests for compare_ink (rendered-ink diff between two page G-code builds).

Run: python3 scripts/test_compare_ink.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_ink import compare_pages  # noqa: E402

ZD, ZU = 74.2, 80.2


def page(strokes: list[list[tuple[float, float]]], z_up: float = ZU) -> str:
    out = []
    for s in strokes:
        out += [f"G0 X{s[0][0]:.3f} Y{s[0][1]:.3f} F30000", f"G1 Z{ZD:.3f} F1800"]
        out += [f"G1 X{x:.3f} Y{y:.3f} F6000" for x, y in s[1:]]
        out.append(f"G1 Z{z_up:.3f} F1800")
    return "\n".join(out) + "\n"


BASE = [[(100, 100), (120, 100)], [(100, 110), (100, 130)], [(130, 130), (130.4, 130)]]


def test_identical_pages_match() -> None:
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.gcode", Path(td) / "b.gcode"
        a.write_text(page(BASE))
        b.write_text(page(BASE))
        r = compare_pages(a, b)
        assert r.iou > 0.999, r
        assert r.missing_pct == 0 and r.extra_pct == 0, r


def test_missing_dot_is_reported() -> None:
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.gcode", Path(td) / "b.gcode"
        a.write_text(page(BASE))
        b.write_text(page(BASE[:2]))
        r = compare_pages(a, b)
        assert r.missing_pct > 1.0, r
        assert r.extra_pct == 0, r


def test_subpixel_shift_is_not_missing_ink() -> None:
    shifted = [[(x + 0.04, y) for x, y in s] for s in BASE]
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.gcode", Path(td) / "b.gcode"
        a.write_text(page(BASE))
        b.write_text(page(shifted))
        r = compare_pages(a, b)
        assert r.missing_pct == 0 and r.extra_pct == 0, r


def test_builds_with_different_hop_compare_by_their_own_levels() -> None:
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.gcode", Path(td) / "b.gcode"
        a.write_text(page(BASE))
        b.write_text(page(BASE, z_up=ZD + 2.5))
        r = compare_pages(a, b)
        assert r.iou > 0.999 and r.strokes_ref == r.strokes_new == 3, r


def test_empty_pages_do_not_crash() -> None:
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.gcode", Path(td) / "b.gcode"
        a.write_text(page(BASE))
        b.write_text("")
        r = compare_pages(a, b)
        assert r.missing_pct == 100.0 and r.strokes_new == 0, r
        a.write_text("")
        r = compare_pages(a, b)
        assert r.iou == 1.0 and r.missing_pct == 0.0, r


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_compare_ink: {len(tests)} passed")


if __name__ == "__main__":
    main()
