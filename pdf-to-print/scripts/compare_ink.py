#!/usr/bin/env python3
"""Compare the ink two page G-code builds would put on paper (quality gate for path changes).

Each page is rendered with a round pen of PEN_MM width. Reported per page:
  iou          overlap of the two ink masks;
  missing_pct  share of reference ink farther than TOLERANCE_MM from any candidate ink
               (lost strokes, dots, spurs);
  extra_pct    the same the other way round (ink that was not there before).
Sub-pixel reshaping of a line (smoothing) changes iou slightly but not missing/extra.

Pen-down / pen-up Z are read from each file, so builds with different holders or Z-hop compare
correctly. Strikethrough strokes after the experimental marker are ignored.

Usage:
  python3 scripts/compare_ink.py REF_DIR NEW_DIR
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gcode_stroke_parse import detect_pen_levels, parse_stroke_polylines  # noqa: E402

PEN_MM = 0.5
DPI = 600
TOLERANCE_MM = 0.15
PX_PER_MM = DPI / 25.4


@dataclass
class InkDiff:
    iou: float
    missing_pct: float
    extra_pct: float
    strokes_ref: int
    strokes_new: int


def load_strokes(path: Path) -> list[np.ndarray]:
    """Pen strokes of a page file; [] for a page without strokes."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"^G0", text, re.M):
        return []
    z_pen, z_up = detect_pen_levels(path)
    strokes = [s for s in parse_stroke_polylines(path, z_pen, z_up) if len(s)]
    travels = len(re.findall(r"^G0 ", text.split("; === pdf-to-print experimental")[0], re.M))
    if travels > 1 and len(strokes) <= 1:
        raise ValueError(f"{path}: {travels} travels but {len(strokes)} stroke parsed — "
                         f"unexpected pen levels Z{z_pen:g}/Z{z_up:g}")
    return strokes


def _render(strokes: list[np.ndarray], origin: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    im = Image.new("L", size, 0)
    d = ImageDraw.Draw(im)
    w = max(1, round(PEN_MM * PX_PER_MM))
    r = w / 2.0
    for s in strokes:
        pts = [((p.real - origin[0]) * PX_PER_MM, (p.imag - origin[1]) * PX_PER_MM) for p in s]
        if len(pts) > 1:
            d.line(pts, fill=255, width=w, joint="curve")
        for x, y in (pts[0], pts[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return np.asarray(im) > 127


def _canvas(strokes: list[np.ndarray]) -> tuple[np.ndarray, tuple[int, int]]:
    pts = np.concatenate(strokes)
    lo = np.array([pts.real.min(), pts.imag.min()]) - 2.0
    hi = np.array([pts.real.max(), pts.imag.max()]) + 2.0
    size = tuple(int(v) for v in np.ceil((hi - lo) * PX_PER_MM))
    return lo, size


def _far_share(src: np.ndarray, other: np.ndarray) -> float:
    if not src.any():
        return 0.0
    if not other.any():
        return 100.0
    dist_px = distance_transform_edt(~other)
    return float(np.mean(dist_px[src] > TOLERANCE_MM * PX_PER_MM) * 100.0)


def compare_pages(ref: Path, new: Path) -> InkDiff:
    a, b = load_strokes(ref), load_strokes(new)
    if not a and not b:
        return InkDiff(1.0, 0.0, 0.0, 0, 0)
    origin, size = _canvas(a + b)
    ma, mb = _render(a, origin, size), _render(b, origin, size)
    union = np.logical_or(ma, mb).sum()
    iou = float(np.logical_and(ma, mb).sum() / union) if union else 1.0
    return InkDiff(iou, _far_share(ma, mb), _far_share(mb, ma), len(a), len(b))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ref_dir", type=Path)
    p.add_argument("new_dir", type=Path)
    args = p.parse_args()

    worst_missing = worst_extra = 0.0
    for ref in sorted(args.ref_dir.glob("page_*.gcode")):
        new = args.new_dir / ref.name
        if not new.exists():
            sys.exit(f"ERROR: {new} missing")
        try:
            r = compare_pages(ref, new)
        except ValueError as exc:
            sys.exit(f"ERROR: {exc}")
        worst_missing, worst_extra = max(worst_missing, r.missing_pct), max(worst_extra, r.extra_pct)
        print(f"{ref.name}: iou {r.iou:.3f} | missing {r.missing_pct:.2f}% | extra {r.extra_pct:.2f}% "
              f"| strokes {r.strokes_ref} -> {r.strokes_new}")
    print(f"worst: missing {worst_missing:.2f}% extra {worst_extra:.2f}% (ink farther than "
          f"{TOLERANCE_MM} mm from the other build)")


if __name__ == "__main__":
    main()
