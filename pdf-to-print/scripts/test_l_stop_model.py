#!/usr/bin/env python3
"""Tests for l_stop_model: printable L-stop jig (watertight STL, fence faces at the paper edges).

Run: python3 scripts/test_l_stop_model.py
"""

from __future__ import annotations

import math
import struct
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svg_to_gcode as plotter  # noqa: E402
from l_stop_model import JigParams, build_mesh, jig_outline, write_stl  # noqa: E402


def read_stl(path: Path) -> np.ndarray:
    data = path.read_bytes()
    n = struct.unpack_from("<I", data, 80)[0]
    assert len(data) == 84 + 50 * n, (len(data), n)
    tris = np.zeros((n, 3, 3))
    for i in range(n):
        vals = struct.unpack_from("<12f", data, 84 + 50 * i)
        tris[i] = np.array(vals[3:]).reshape(3, 3)
    return tris


def signed_volume(tris: np.ndarray) -> float:
    return float(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6.0)


def test_mesh_is_watertight_and_consistently_oriented() -> None:
    tris = build_mesh(JigParams())
    directed = Counter()
    for t in np.round(tris, 5):
        pts = [tuple(p) for p in t]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            directed[(pts[a], pts[b])] += 1
    for (a, b), k in directed.items():
        assert k == 1, ("duplicate directed edge", a, b, k)
        assert directed.get((b, a), 0) == 1, ("open edge", a, b)


def test_volume_matches_outline_minus_magnet_pockets() -> None:
    p = JigParams()
    outline = jig_outline(p)
    pockets = len(p.magnet_xy) * math.pi * (p.magnet_d / 2 + p.magnet_clearance) ** 2 * p.pocket_depth
    expect = outline.area * p.height - pockets
    vol = signed_volume(build_mesh(p))
    assert vol > 0, vol
    assert abs(vol - expect) / expect < 0.01, (vol, expect)


def test_fence_faces_sit_exactly_on_the_paper_edges() -> None:
    p = JigParams()
    tris = build_mesh(p).reshape(-1, 3)
    x_fence = tris[np.isclose(tris[:, 0], plotter.PAPER_LEFT, atol=1e-6)]
    y_fence = tris[np.isclose(tris[:, 1], plotter.PAPER_FRONT, atol=1e-6)]
    # straight fence faces run from the corner relief to the rounded arm ends
    assert x_fence[:, 1].max() >= p.left_length - p.outer_r - 1e-6, x_fence[:, 1].max()
    assert x_fence[:, 1].min() <= plotter.PAPER_FRONT + p.corner_relief_r + 1e-6
    assert y_fence[:, 0].max() >= p.front_length - p.outer_r - 1e-6, y_fence[:, 0].max()
    assert y_fence[:, 0].min() <= plotter.PAPER_LEFT + p.corner_relief_r + 1e-6
    assert tris[:, 0].min() >= -1e-9 and tris[:, 1].min() >= -1e-9
    assert np.isclose(tris[:, 2].max(), p.height) and np.isclose(tris[:, 2].min(), 0.0)


def test_magnet_pockets_leave_walls_and_a_top_skin() -> None:
    p = JigParams()
    outline = jig_outline(p)
    from shapely.geometry import Point

    r = p.magnet_d / 2 + p.magnet_clearance
    for x, y in p.magnet_xy:
        assert outline.buffer(-p.min_wall).contains(Point(x, y).buffer(r)), (x, y)
    assert p.height - p.pocket_depth >= 0.8


def test_stl_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "l_stop.stl"
        tris = build_mesh(JigParams())
        write_stl(tris, out)
        back = read_stl(out)
        assert back.shape == tris.shape
        assert np.allclose(back, tris, atol=1e-4)


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_l_stop_model: {len(tests)} passed")


if __name__ == "__main__":
    main()
