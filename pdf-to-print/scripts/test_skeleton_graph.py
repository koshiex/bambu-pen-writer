#!/usr/bin/env python3
"""Smoke tests for skeleton_graph: skeleton pixels → minimal pen-lift strokes.

Run: python3 scripts/test_skeleton_graph.py   (bare asserts; pytest-compatible names)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from skimage.draw import circle_perimeter, line
from skimage.morphology import skeletonize

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skeleton_graph import TraceParams, trace_strokes  # noqa: E402

RAW = TraceParams(smooth_sigma_px=0.0, simplify_px=0.0)


def canvas(h: int = 80, w: int = 80) -> np.ndarray:
    return np.zeros((h, w), dtype=bool)


def draw_line(img: np.ndarray, r0: int, c0: int, r1: int, c1: int) -> None:
    rr, cc = line(r0, c0, r1, c1)
    img[rr, cc] = True


def point_to_polyline_dist(p: np.ndarray, poly: np.ndarray) -> float:
    if len(poly) == 1:
        return float(np.hypot(*(poly[0] - p)))
    a, b = poly[:-1], poly[1:]
    ab = b - a
    l2 = np.maximum((ab ** 2).sum(1), 1e-12)
    t = np.clip(((p - a) * ab).sum(1) / l2, 0.0, 1.0)
    proj = a + t[:, None] * ab
    return float(np.min(np.hypot(*(proj - p).T)))


def dist_to_strokes(p: np.ndarray, strokes: list[np.ndarray]) -> float:
    return min(point_to_polyline_dist(p, s) for s in strokes)


def endpoints(s: np.ndarray) -> set[tuple[int, int]]:
    return {tuple(np.round(s[0]).astype(int)), tuple(np.round(s[-1]).astype(int))}


def near(a: np.ndarray, xy: tuple[float, float], tol: float = 1.01) -> bool:
    return float(np.hypot(a[0] - xy[0], a[1] - xy[1])) <= tol


def test_straight_line_single_trail() -> None:
    img = canvas()
    draw_line(img, 10, 10, 10, 60)
    strokes = trace_strokes(img, RAW)
    assert len(strokes) == 1, len(strokes)
    s = strokes[0]
    assert {tuple(s[0]), tuple(s[-1])} == {(10.0, 10.0), (60.0, 10.0)}, (s[0], s[-1])


def test_t_junction_two_trails() -> None:
    img = canvas()
    draw_line(img, 10, 10, 10, 60)
    draw_line(img, 10, 35, 50, 35)
    strokes = trace_strokes(img, RAW)
    assert len(strokes) == 2, len(strokes)
    total = sum(float(np.sum(np.hypot(*np.diff(s, axis=0).T))) for s in strokes)
    assert abs(total - 90.0) < 3.0, total


def test_x_crossing_goes_straight_through() -> None:
    img = canvas()
    draw_line(img, 30, 5, 30, 55)
    draw_line(img, 5, 30, 55, 30)
    strokes = trace_strokes(img, RAW)
    assert len(strokes) == 2, len(strokes)
    for s in strokes:
        a, b = s[0], s[-1]
        straight_h = near(a, (5, 30)) and near(b, (55, 30)) or near(a, (55, 30)) and near(b, (5, 30))
        straight_v = near(a, (30, 5)) and near(b, (30, 55)) or near(a, (30, 55)) and near(b, (30, 5))
        assert straight_h or straight_v, (a, b)


def test_ring_is_one_closed_trail() -> None:
    img = canvas()
    rr, cc = circle_perimeter(40, 40, 15)
    img[rr, cc] = True
    strokes = trace_strokes(img, RAW)
    assert len(strokes) == 1, len(strokes)
    assert np.allclose(strokes[0][0], strokes[0][-1]), (strokes[0][0], strokes[0][-1])


def test_loop_with_tail_is_one_trail() -> None:
    img = canvas()
    rr, cc = circle_perimeter(30, 40, 12)
    img[rr, cc] = True
    draw_line(img, 42, 40, 70, 40)
    strokes = trace_strokes(img, RAW)
    assert len(strokes) == 1, len(strokes)


def test_short_spur_is_pruned() -> None:
    img = canvas()
    draw_line(img, 20, 5, 20, 60)
    draw_line(img, 17, 30, 19, 30)
    strokes = trace_strokes(img, TraceParams(spur_px=4.0, smooth_sigma_px=0.0, simplify_px=0.0))
    assert len(strokes) == 1, len(strokes)
    assert min(float(s[:, 1].min()) for s in strokes) >= 19.5


def test_long_branch_is_kept() -> None:
    img = canvas()
    draw_line(img, 40, 5, 40, 70)
    draw_line(img, 20, 35, 39, 35)
    strokes = trace_strokes(img, RAW)
    assert len(strokes) == 2, len(strokes)
    assert dist_to_strokes(np.array([35.0, 20.0]), strokes) <= 0.5


def test_staircase_has_no_false_junctions() -> None:
    img = canvas()
    r, c = 5, 5
    for _ in range(30):
        img[r, c] = True
        img[r, c + 1] = True
        r, c = r + 1, c + 1
    strokes = trace_strokes(img, RAW)
    assert len(strokes) == 1, len(strokes)


def test_short_stub_is_retraced_instead_of_lifting() -> None:
    img = canvas()
    draw_line(img, 40, 5, 40, 70)
    draw_line(img, 32, 35, 39, 35)
    no_retrace = trace_strokes(img, TraceParams(spur_px=2.0, retrace_px=0.0,
                                                smooth_sigma_px=0.0, simplify_px=0.0))
    with_retrace = trace_strokes(img, TraceParams(spur_px=2.0, retrace_px=12.0,
                                                  smooth_sigma_px=0.0, simplify_px=0.0))
    assert len(no_retrace) == 2, len(no_retrace)
    assert len(with_retrace) == 1, len(with_retrace)
    assert dist_to_strokes(np.array([35.0, 32.0]), with_retrace) <= 0.5


def test_trail_count_matches_euler_bound_for_components() -> None:
    img = canvas(120, 120)
    draw_line(img, 10, 10, 10, 60)          # line: 1
    draw_line(img, 30, 10, 30, 60)          # T: 2
    draw_line(img, 30, 35, 60, 35)
    draw_line(img, 90, 70, 90, 110)         # X: 2
    draw_line(img, 70, 90, 110, 90)
    rr, cc = circle_perimeter(40, 90, 12)   # ring: 1
    img[rr, cc] = True
    strokes = trace_strokes(img, RAW)
    assert len(strokes) == 6, len(strokes)


def test_isolated_pixel_is_ignored() -> None:
    img = canvas()
    img[5, 5] = True
    assert trace_strokes(img, RAW) == []


def test_smoothing_bounds_deviation_and_reduces_points() -> None:
    img = canvas(120, 120)
    rr, cc = line(10, 10, 100, 45)
    img[rr, cc] = True
    raw = trace_strokes(img, RAW)
    smooth = trace_strokes(img, TraceParams())
    assert len(raw) == 1 and len(smooth) == 1
    assert len(smooth[0]) * 3 < len(raw[0]), (len(smooth[0]), len(raw[0]))
    worst = max(point_to_polyline_dist(p, smooth[0]) for p in raw[0])
    assert worst <= 0.75, worst
    assert np.allclose(smooth[0][0], raw[0][0]) or np.allclose(smooth[0][0], raw[0][-1])


def test_rasterized_glyph_is_fully_covered() -> None:
    im = Image.new("L", (160, 120), 0)
    d = ImageDraw.Draw(im)
    d.ellipse((15, 15, 75, 95), outline=255, width=6)
    d.line((75, 20, 75, 100), fill=255, width=6)
    d.line((90, 20, 140, 100), fill=255, width=6)
    d.line((140, 20, 90, 100), fill=255, width=6)
    d.line((85, 60, 150, 60), fill=255, width=6)
    skel = skeletonize(np.asarray(im) > 127)
    strokes = trace_strokes(skel, TraceParams())
    ys, xs = np.nonzero(skel)
    pts = np.stack([xs, ys], axis=1).astype(float)
    covered = np.mean([dist_to_strokes(p, strokes) <= 1.5 for p in pts])
    assert covered >= 0.97, covered
    comps = 2
    assert len(strokes) <= comps + 6, len(strokes)


def _densify(poly: np.ndarray, step: float = 0.25) -> np.ndarray:
    parts = [poly[:1]]
    for a, b in zip(poly[:-1], poly[1:]):
        n = max(1, int(np.ceil(np.hypot(*(b - a)) / step)))
        parts.append(a + (b - a) * np.linspace(0, 1, n + 1)[1:, None])
    return np.concatenate(parts)


def test_pen_down_path_never_leaves_the_ink() -> None:
    from scipy.ndimage import distance_transform_edt

    im = Image.new("L", (160, 120), 0)
    d = ImageDraw.Draw(im)
    d.ellipse((15, 15, 75, 95), outline=255, width=6)
    d.line((75, 20, 75, 100), fill=255, width=6)
    d.line((90, 20, 140, 100), fill=255, width=6)
    d.line((140, 20, 90, 100), fill=255, width=6)
    d.line((85, 60, 150, 60), fill=255, width=6)
    ink = np.asarray(im) > 127
    outside = distance_transform_edt(~ink)
    for s in trace_strokes(skeletonize(ink), TraceParams()):
        p = _densify(s)
        rows = np.clip(np.round(p[:, 1]).astype(int), 0, ink.shape[0] - 1)
        cols = np.clip(np.round(p[:, 0]).astype(int), 0, ink.shape[1] - 1)
        assert outside[rows, cols].max() <= 1.0, outside[rows, cols].max()


def test_smoothed_ring_stays_closed_and_on_the_pixels() -> None:
    img = canvas()
    rr, cc = circle_perimeter(40, 40, 15)
    img[rr, cc] = True
    raw = trace_strokes(img, RAW)
    smooth = trace_strokes(img, TraceParams())
    assert len(smooth) == 1 and np.allclose(smooth[0][0], smooth[0][-1])
    worst = max(point_to_polyline_dist(p, smooth[0]) for p in raw[0])
    assert worst <= 0.75, worst


def test_last_retrace_pair_is_not_wasted_in_multi_edge_component() -> None:
    img = canvas()
    draw_line(img, 20, 5, 20, 45)           # bar: arms of 20 px either side of the stem
    draw_line(img, 21, 25, 28, 25)          # stem: 8 px
    strokes = trace_strokes(img, TraceParams(spur_px=2.0, retrace_px=30.0,
                                             smooth_sigma_px=0.0, simplify_px=0.0))
    assert len(strokes) == 1, len(strokes)
    total = sum(float(np.sum(np.hypot(*np.diff(s, axis=0).T))) for s in strokes)
    assert total < 60.0, total              # bar once + stem twice ≈ 56, not a closed 96


def test_isolated_short_dash_is_drawn_out_and_back() -> None:
    """Dots/commas: skeleton of 2–3 px would not survive min_points / the 0.3 mm filter."""
    img = canvas()
    img[10, 5:8] = True
    strokes = trace_strokes(img, TraceParams(min_points=4, smooth_sigma_px=0.0, simplify_px=0.0))
    assert len(strokes) == 1, strokes
    assert len(strokes[0]) == 5 and np.allclose(strokes[0][0], strokes[0][-1]), strokes[0]


def _run_png_cli(png: Path, svg: Path, topology: str) -> list[str]:
    import subprocess
    import xml.etree.ElementTree as ET

    script = Path(__file__).resolve().parent / "png_to_skeleton_svg.py"
    subprocess.run(
        [sys.executable, str(script), str(png), str(svg), "--topology", topology,
         "--min-polyline-points", "4"],
        check=True, capture_output=True, text=True,
    )
    ns = "{http://www.w3.org/2000/svg}"
    return [p.get("d") for p in ET.parse(svg).getroot().iter(f"{ns}path")]


def test_png_cli_euler_needs_fewer_paths_than_legacy() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        im = Image.new("L", (600, 400), 255)
        d = ImageDraw.Draw(im)
        d.line((50, 60, 550, 60), fill=0, width=9)      # T
        d.line((300, 60, 300, 340), fill=0, width=9)
        d.ellipse((80, 150, 220, 330), outline=0, width=9)
        im.save(t / "p.png")
        legacy = _run_png_cli(t / "p.png", t / "legacy.svg", "legacy")
        euler = _run_png_cli(t / "p.png", t / "euler.svg", "euler")
        assert len(euler) == 3, len(euler)
        assert len(legacy) > len(euler), (len(legacy), len(euler))
        xs = [float(tok.lstrip("ML")) for d in euler for tok in d.split()[0::2]]
        assert 0.0 <= min(xs) and max(xs) <= 623.68, (min(xs), max(xs))


def main() -> None:
    tests =[(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_skeleton_graph: {len(tests)} passed")


if __name__ == "__main__":
    main()
