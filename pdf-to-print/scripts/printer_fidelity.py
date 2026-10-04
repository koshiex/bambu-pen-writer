#!/usr/bin/env python3
"""Compare the ink a job would draw with the source PDF page raster (used by printer_sim.py).

The ink of each simulated page (pen frame, strikethrough decorations excluded) is mapped back
onto the portrait source page with the exact vpype transform of svg_to_gcode (fitted from a
probe SVG, so any change of paper origin / holder offset is picked up automatically):

  INK_OUTSIDE_GLYPH  ink farther than MAX_STRAY_MM from any glyph pixel, or more than
                     OUTSIDE_SHARE_MAX of ink points farther than OUTSIDE_TOL_MM (stray lines).
  TEXT_MISSING       a connected glyph component (letter, dot of й/ё, comma) gets no ink at all.
  COVERAGE_LOW       more than UNCOVERED_SHARE_MAX of the glyph skeleton is farther than
                     COVER_TOL_MM from the ink (parts of letters not drawn).
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svg_to_gcode as plotter  # noqa: E402
from png_to_skeleton_svg import (  # noqa: E402
    DEFAULT_HEIGHT_UU,
    DEFAULT_WIDTH_UU,
    SVG_UU_PER_MM,
    load_binary_mask,
)
from printer_sim import Issue, PageStats, SimReport  # noqa: E402

OUTSIDE_TOL_MM = 0.25        # experimental word jitter moves ink up to 0.12 mm
OUTSIDE_SHARE_MAX = 0.005
MAX_STRAY_MM = 0.5
COVER_TOL_MM = 0.3
UNCOVERED_SHARE_MAX = 0.02
DENSIFY_MM = 0.05
MAX_SPECK_PX, MAX_HOLE_PX = 16, 64   # same as png_to_skeleton_svg defaults


@dataclass
class PageAffine:
    """SVG user units of the portrait page ↔ nozzle frame (mm) of the emitted G-code."""
    to_nozzle: np.ndarray       # 3×2: [u, v, 1] @ M = [x, y]
    offset: tuple[float, float]

    @classmethod
    def for_holder(cls, holder: str) -> "PageAffine":
        import vpype as vp
        from vpype_cli import execute

        plotter.apply_holder_profile(soft_holder=holder == "soft")
        src = np.array([[10.0, 20.0], [600.0, 20.0], [10.0, 700.0]])
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe.svg"
            probe.write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{DEFAULT_WIDTH_UU}" '
                f'height="{DEFAULT_HEIGHT_UU}" viewBox="0 0 {DEFAULT_WIDTH_UU} {DEFAULT_HEIGHT_UU}">'
                f'<path d="M{src[0][0]} {src[0][1]} L{src[1][0]} {src[1][1]} L{src[2][0]} {src[2][1]}" '
                f'stroke="black" fill="none"/></svg>')
            doc = execute(f"read --single-layer {probe} " + " ".join(plotter.page_transform_commands()))
        line = next(iter(doc.layers.values())).lines[0] / vp.convert_length("1mm")
        dst = np.c_[line.real, line.imag]
        m, *_ = np.linalg.lstsq(np.c_[src, np.ones(3)], dst, rcond=None)
        return cls(m, (plotter.PEN_OFFSET_X, plotter.PEN_OFFSET_Y))

    def uu_to_nozzle(self, u: float, v: float) -> tuple[float, float]:
        x, y = np.array([u, v, 1.0]) @ self.to_nozzle
        return float(x), float(y)

    def uu_to_pen(self, u: float, v: float) -> tuple[float, float]:
        x, y = self.uu_to_nozzle(u, v)
        return x + self.offset[0], y + self.offset[1]

    def nozzle_to_uu(self, x: float, y: float) -> tuple[float, float]:
        u, v = self.nozzle_to_uu_array(np.array([[x, y]]))[0]
        return float(u), float(v)

    def nozzle_to_uu_array(self, xy: np.ndarray) -> np.ndarray:
        lin, t = self.to_nozzle[:2], self.to_nozzle[2]
        return (xy - t) @ np.linalg.inv(lin)

    def pen_to_uu_array(self, xy: np.ndarray) -> np.ndarray:
        return self.nozzle_to_uu_array(xy - np.asarray(self.offset))


@dataclass
class FidelityResult:
    page: int
    max_outside_mm: float = 0.0
    outside_share: float = 0.0
    uncovered_share: float = 0.0
    missing_components: int = 0
    components: int = 0
    errors: list[Issue] = field(default_factory=list)
    missing_at_mm: list[tuple[float, float]] = field(default_factory=list)


def _densify(poly_px: np.ndarray, step_px: float) -> np.ndarray:
    parts = [poly_px[:1]]
    for a, b in zip(poly_px[:-1], poly_px[1:]):
        n = max(1, int(np.ceil(np.hypot(*(b - a)) / step_px)))
        parts.append(a + (b - a) * np.linspace(0, 1, n + 1)[1:, None])
    return np.concatenate(parts)


def _ink_pixels(page: PageStats, aff: PageAffine, px_per_uu: float, px_per_mm: float) -> np.ndarray:
    pts = [_densify(aff.pen_to_uu_array(s) * px_per_uu, DENSIFY_MM * px_per_mm)
           for s in page.strokes if len(s) >= 1]
    return np.concatenate(pts) if pts else np.zeros((0, 2))


def page_fidelity(page: PageStats, png: Path, aff: PageAffine,
                  render: Path | None = None) -> FidelityResult:
    mask = load_binary_mask(png, MAX_SPECK_PX, MAX_HOLE_PX)
    h, w = mask.shape
    px_per_uu = w / DEFAULT_WIDTH_UU
    px_per_mm = px_per_uu * SVG_UU_PER_MM
    res = FidelityResult(page.number)
    ink = _ink_pixels(page, aff, px_per_uu, px_per_mm)
    cols = np.clip(np.round(ink[:, 0]).astype(int), 0, w - 1)
    rows = np.clip(np.round(ink[:, 1]).astype(int), 0, h - 1)
    far = np.zeros(len(ink), dtype=bool)
    if len(ink):
        d_out = ndimage.distance_transform_edt(~mask)[rows, cols] / px_per_mm
        far = d_out > OUTSIDE_TOL_MM
        res.max_outside_mm = float(d_out.max())
        res.outside_share = float(np.mean(far))
    ink_img = np.zeros_like(mask)
    ink_img[rows, cols] = True
    d_ink = ndimage.distance_transform_edt(~ink_img) / px_per_mm
    skel = _skeleton(mask)
    if skel.any():
        res.uncovered_share = float(np.mean(d_ink[skel] > COVER_TOL_MM))
    labels, n = ndimage.label(mask, structure=np.ones((3, 3)))
    res.components = n
    missing: list[int] = []
    if n:
        nearest = ndimage.minimum(d_ink, labels, index=np.arange(1, n + 1))
        missing = [i + 1 for i, d in enumerate(nearest) if d > COVER_TOL_MM]
        res.missing_components = len(missing)
        centers = ndimage.center_of_mass(mask, labels, missing) if missing else []
        res.missing_at_mm = [(c[1] / px_per_mm, c[0] / px_per_mm) for c in centers[:10]]
    _judge(res)
    if render is not None:
        _render(mask, labels, missing, (rows, cols), far, render)
    return res


def _skeleton(mask: np.ndarray) -> np.ndarray:
    from skimage.morphology import skeletonize
    return skeletonize(mask)


def _judge(res: FidelityResult) -> None:
    p = res.page
    if res.max_outside_mm > MAX_STRAY_MM or res.outside_share > OUTSIDE_SHARE_MAX:
        res.errors.append(Issue("INK_OUTSIDE_GLYPH", f"page {p:02d}: ink up to {res.max_outside_mm:.2f} mm "
                                f"outside glyphs, {res.outside_share:.2%} of ink > {OUTSIDE_TOL_MM} mm", 0))
    if res.missing_components:
        where = ", ".join(f"({x:.1f},{y:.1f})" for x, y in res.missing_at_mm)
        res.errors.append(Issue("TEXT_MISSING", f"page {p:02d}: {res.missing_components} glyph parts "
                                f"without ink, page mm (x from left, y from top): {where}", 0))
    if res.uncovered_share > UNCOVERED_SHARE_MAX:
        res.errors.append(Issue("COVERAGE_LOW", f"page {p:02d}: {res.uncovered_share:.2%} of glyph "
                                f"skeleton farther than {COVER_TOL_MM} mm from ink", 0))


def _render(mask: np.ndarray, labels: np.ndarray, missing: list[int],
            ink_rc: tuple[np.ndarray, np.ndarray], far: np.ndarray, out: Path) -> None:
    """Grey = source glyphs, black = ink, blue = glyph parts without ink, red = ink off glyphs."""
    rows, cols = ink_rc
    img = np.full(mask.shape + (3,), 255, np.uint8)
    img[mask] = (205, 205, 205)
    if missing:
        img[np.isin(labels, missing)] = (40, 90, 255)
    img[rows, cols] = (0, 0, 0)
    img[rows[far], cols[far]] = (230, 0, 0)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out)


def fidelity_section(report: SimReport, holder: str, png_dir: Path,
                     render_dir: Path | None) -> tuple[str, list[Issue]]:
    aff = PageAffine.for_holder(holder)
    lines = ["", "## Fidelity vs source PDF", "",
             "| page | components | missing | max outside mm | outside share | uncovered skeleton |",
             "|---|---|---|---|---|---|"]
    errors: list[Issue] = []
    for page in (p for p in report.pages if p.number > 0):
        png = png_dir / f"page_{page.number:02d}.png"
        if not png.exists():
            errors.append(Issue("FIDELITY_NO_SOURCE", f"missing {png}", 0))
            continue
        out = render_dir / f"page_{page.number:02d}.png" if render_dir else None
        r = page_fidelity(page, png, aff, out)
        errors.extend(r.errors)
        lines.append(f"| {r.page:02d} | {r.components} | {r.missing_components} | {r.max_outside_mm:.2f} | "
                     f"{r.outside_share:.3%} | {r.uncovered_share:.2%} |")
    lines += ["", f"Fidelity errors: {len(errors)}"] + [f"- `{e.code}`: {e.message}" for e in errors[:50]]
    return "\n".join(lines) + "\n", errors
