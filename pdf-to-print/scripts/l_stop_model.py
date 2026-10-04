#!/usr/bin/env python3
"""Printable L-stop jig for placing the notebook on the P1S plate (STL, no CAD needed).

The jig is a flat L, `height` mm tall, lying on the build plate with its outer edges flush with
the plate's left and front edges. Its inner faces are exactly where the paper must start:
x = PAPER_LEFT, y = PAPER_FRONT (svg_to_gcode.py, the same constants the G-code is built
from). The notebook is pushed into the inner corner (spine / bottom edge against the faces).

Why it can stay on the plate for the whole notebook:
  * it lies outside the paper, so the pen tip never goes over it;
  * at 3 mm it is no taller than a folded-back 12-sheet notebook, so the holder body — which is
    always above the paper surface — cannot reach it; printer_sim.py --l-stop-height checks it
    against the real job (and shows the margin for a single loose sheet);
  * magnets in bottom pockets hold it to the steel plate, so it does not move between pages.

Inner corner has a small round relief so the notebook corner seats fully. Print flat, bottom
face down, PLA/PETG, 100 % size; press magnets (default 10×2 mm discs) into the pockets.

Usage:
  python3 scripts/l_stop_model.py                       # output/l_stop.stl + preview PNG
  python3 scripts/l_stop_model.py --height 2.5 --magnet-d 8 --magnet-h 3
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svg_to_gcode as plotter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUAD_SEGS = 8


@dataclass(frozen=True)
class JigParams:
    height: float = 3.0                 # fence height above the plate
    front_length: float = 190.0         # front arm along X (paper bottom edge 24..229)
    left_length: float = 190.0          # left arm along Y (paper spine edge 50..215)
    outer_r: float = 4.0                # rounding of convex corners
    corner_relief_r: float = 2.0        # round relief in the inner corner
    magnet_d: float = 10.0              # magnet disc diameter
    magnet_h: float = 2.0               # magnet disc thickness
    magnet_clearance: float = 0.15      # radial press-fit clearance
    min_wall: float = 3.0               # material around a pocket
    magnet_xy: tuple[tuple[float, float], ...] = (
        (12.0, 25.0), (75.0, 25.0), (135.0, 25.0), (180.0, 25.0), (12.0, 100.0), (12.0, 165.0))

    @property
    def pocket_depth(self) -> float:
        return self.magnet_h + 0.1

    @property
    def pocket_r(self) -> float:
        return self.magnet_d / 2 + self.magnet_clearance


def jig_outline(p: JigParams) -> Polygon:
    """L in plate coordinates (origin = plate front-left corner), without magnet pockets."""
    fence_x, fence_y = plotter.PAPER_LEFT, plotter.PAPER_FRONT
    shape = unary_union([box(0, 0, p.front_length, fence_y), box(0, 0, fence_x, p.left_length)])
    shape = shape.buffer(-p.outer_r, quad_segs=QUAD_SEGS).buffer(p.outer_r, quad_segs=QUAD_SEGS)
    shape = shape.difference(Point(fence_x, fence_y).buffer(p.corner_relief_r, quad_segs=QUAD_SEGS))
    return orient(shape, sign=1.0)


def _pockets(p: JigParams) -> list[Polygon]:
    return [orient(Point(x, y).buffer(p.pocket_r, quad_segs=QUAD_SEGS * 2), sign=1.0)
            for x, y in p.magnet_xy]


def _faces(poly: Polygon, z: float, up: bool) -> list[np.ndarray]:
    out = []
    for tri in shapely.constrained_delaunay_triangles(poly).geoms:
        xy = np.asarray(tri.exterior.coords)[:3]
        ccw = (xy[1, 0] - xy[0, 0]) * (xy[2, 1] - xy[0, 1]) - (xy[1, 1] - xy[0, 1]) * (xy[2, 0] - xy[0, 0]) > 0
        if ccw != up:
            xy = xy[::-1]
        out.append(np.c_[xy, np.full(3, z)])
    return out


def _walls(ring: np.ndarray, z0: float, z1: float) -> list[np.ndarray]:
    """Quads along a ring; outward normal on the right of the ring direction."""
    out = []
    for a, b in zip(ring[:-1], ring[1:]):
        a0, b0, a1, b1 = (*a, z0), (*b, z0), (*a, z1), (*b, z1)
        out += [np.array([a0, b0, b1]), np.array([a0, b1, a1])]
    return out


def build_mesh(p: JigParams) -> np.ndarray:
    """Watertight triangle mesh (N, 3, 3): solid L with blind magnet pockets in the bottom."""
    outline = jig_outline(p)
    pockets = _pockets(p)
    bottom = orient(outline.difference(unary_union(pockets)), sign=1.0)
    tris = _faces(outline, p.height, up=True) + _faces(bottom, 0.0, up=False)
    tris += _walls(np.asarray(outline.exterior.coords), 0.0, p.height)
    for hole in bottom.interiors:
        tris += _walls(np.asarray(hole.coords), 0.0, p.pocket_depth)
    for pocket in pockets:
        tris += _faces(pocket, p.pocket_depth, up=False)
    return np.asarray(tris)


def write_stl(tris: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(b"pdf-to-print L-stop jig".ljust(80, b" "))
        fh.write(struct.pack("<I", len(tris)))
        for t in tris:
            n = np.cross(t[1] - t[0], t[2] - t[0])
            n = n / (np.linalg.norm(n) or 1.0)
            fh.write(struct.pack("<12fH", *n, *t.reshape(-1), 0))


def write_preview(p: JigParams, path: Path, px_per_mm: float = 3.0) -> None:
    """Top view: plate edge, jig, magnet pockets, paper (dashed) — for checking orientation."""
    from PIL import Image, ImageDraw

    size = 256
    img = Image.new("RGB", (int(size * px_per_mm), int(size * px_per_mm)), "white")
    d = ImageDraw.Draw(img)

    def m(x: float, y: float) -> tuple[float, float]:
        return x * px_per_mm, (size - y) * px_per_mm

    d.rectangle([m(0, size), m(size, 0)], outline=(120, 120, 120), width=2)
    d.polygon([m(x, y) for x, y in jig_outline(p).exterior.coords], fill=(70, 130, 220))
    for pk in _pockets(p):
        d.polygon([m(x, y) for x, y in pk.exterior.coords], outline=(255, 255, 255))
    x0, y0 = plotter.PAPER_LEFT, plotter.PAPER_FRONT
    x1, y1 = x0 + plotter.PAPER_W, y0 + plotter.PAPER_H
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        n = 40
        for i in range(0, n, 2):
            t0, t1 = i / n, (i + 1) / n
            d.line([m(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                    m(a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)], fill=(0, 0, 0), width=2)
    d.text(m(x0 + 60, y0 + 80), "notebook (landscape)", fill=(0, 0, 0))
    d.text(m(60, 20), f"front arm, fence at y={y0:g} mm", fill=(255, 255, 255))
    d.text(m(3, 130), f"x={x0:g}", fill=(255, 255, 255))
    d.text(m(90, 3), "plate FRONT edge (door side)", fill=(80, 80, 80))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def main() -> None:
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--height", type=float, default=JigParams.height)
    a.add_argument("--front-length", type=float, default=JigParams.front_length)
    a.add_argument("--left-length", type=float, default=JigParams.left_length)
    a.add_argument("--magnet-d", type=float, default=JigParams.magnet_d)
    a.add_argument("--magnet-h", type=float, default=JigParams.magnet_h)
    a.add_argument("--out", type=Path, default=ROOT / "output" / "l_stop.stl")
    args = a.parse_args()
    p = replace(JigParams(), height=args.height, front_length=args.front_length,
                left_length=args.left_length, magnet_d=args.magnet_d, magnet_h=args.magnet_h)
    if p.height - p.pocket_depth < 0.8:
        sys.exit(f"ERROR: height {p.height} leaves < 0.8 mm above {p.pocket_depth} mm magnet pockets")
    tris = build_mesh(p)
    write_stl(tris, args.out)
    preview = args.out.with_name(args.out.stem + "_preview.png")
    write_preview(p, preview)
    print(f"✅ {args.out} ({len(tris)} triangles): L {p.front_length:g}×{p.left_length:g} mm, "
          f"height {p.height:g} mm, fences at x={plotter.PAPER_LEFT:g} / y={plotter.PAPER_FRONT:g} "
          f"from the plate's left / front edge, {len(p.magnet_xy)} pockets Ø{2 * p.pocket_r:.1f}×"
          f"{p.pocket_depth:.1f} mm")
    print(f"   preview: {preview}")


if __name__ == "__main__":
    main()
